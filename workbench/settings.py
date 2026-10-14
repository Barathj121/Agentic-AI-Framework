from __future__ import annotations

import os
import ipaddress
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


def validate_local_url(value: str, name: str) -> str:
    """Accept loopback/private LAN literals without DNS or public-cloud endpoints."""
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"{name} must be an HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError(f"{name} cannot contain credentials, a query, or fragment")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError(f"{name} contains an invalid port")
        host = parsed.hostname.rstrip(".").lower()
        if host != "localhost":
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                raise ValueError(f"{name} must use localhost or a literal private LAN/loopback IP address") from None
            allowed_lan = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
                           ipaddress.ip_network("192.168.0.0/16"), ipaddress.ip_network("fc00::/7"))
            if not address.is_loopback and not any(address in network for network in allowed_lan if address.version == network.version):
                raise ValueError(f"{name} must use a local/private endpoint; public internet addresses are disabled")
        if any(ord(character) < 32 for character in value) or "\\" in value:
            raise ValueError(f"{name} contains invalid characters")
    except ValueError as error:
        # urllib can include user input in error messages; never echo credentials.
        if str(error).startswith(name):
            raise
        raise ValueError(f"{name} is not a valid local HTTP(S) URL") from None
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    workspace_root: Path = Path("workspaces")
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    llm_base_url: str = "http://localhost:1234/v1"
    llm_model: str = ""
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""), repr=False)
    llm_structured_mode: str = "json_schema"
    llm_temperature: float = 0.1
    llm_timeout_seconds: int = 180
    llm_context_tokens: int = 8192
    llm_max_output_tokens: int = 4096
    max_upload_mb: int = 100
    sandbox_image: str = "agentic-workbench-sandbox:latest"
    sandbox_cpus: float = 4
    sandbox_memory: str = "8g"
    sandbox_timeout_seconds: int = 300
    max_repair_attempts: int = 2

    def __post_init__(self):
        object.__setattr__(self, "workspace_root", Path(self.workspace_root).resolve())
        if self.llm_provider not in {"ollama", "openai_compatible"}:
            raise ValueError("LLM_PROVIDER must be ollama or openai_compatible")
        if self.llm_structured_mode not in {"json_schema", "json_object"}:
            raise ValueError("LLM_STRUCTURED_MODE must be json_schema or json_object")
        object.__setattr__(self, "ollama_base_url", validate_local_url(self.ollama_base_url, "OLLAMA_BASE_URL"))
        object.__setattr__(self, "llm_base_url", validate_local_url(self.llm_base_url, "LLM_BASE_URL"))
        model = self.ollama_model if self.llm_provider == "ollama" else self.llm_model
        model_variable = "OLLAMA_MODEL" if self.llm_provider == "ollama" else "LLM_MODEL"
        if not model.strip() or any(ord(character) < 32 for character in model):
            raise ValueError(f"{model_variable} must be the exact nonempty model ID reported by the local server")
        if re.search(r"(?:[:/-])cloud$", model, re.IGNORECASE):
            raise ValueError("Cloud models are disabled for this local-first workbench")
        if "\r" in self.llm_api_key or "\n" in self.llm_api_key:
            raise ValueError("LLM_API_KEY must not contain newline characters")
        if not 0 <= self.max_repair_attempts <= 2:
            raise ValueError("MAX_REPAIR_ATTEMPTS must be between 0 and 2")
        for key in ("llm_timeout_seconds", "llm_context_tokens", "llm_max_output_tokens", "max_upload_mb", "sandbox_cpus", "sandbox_timeout_seconds"):
            if getattr(self, key) <= 0:
                raise ValueError(f"{key} must be positive")
        if self.llm_max_output_tokens >= self.llm_context_tokens:
            raise ValueError("LLM_MAX_OUTPUT_TOKENS must be smaller than LLM_CONTEXT_TOKENS")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(Path.cwd() / ".env", override=False)
        defaults = cls()
        values = {}
        for field in fields(cls):
            raw = os.getenv(field.name.upper())
            if raw is not None:
                current = getattr(defaults, field.name)
                values[field.name] = Path(raw) if isinstance(current, Path) else type(current)(raw)
        return cls(**values)
