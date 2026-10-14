from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    workspace_root: Path = Path("workspaces")
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
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
        parsed = urlparse(self.ollama_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("OLLAMA_BASE_URL must be an HTTP(S) URL")
        if self.ollama_model.endswith(":cloud"):
            raise ValueError("Cloud models are disabled for this local-first workbench")
        if not 0 <= self.max_repair_attempts <= 2:
            raise ValueError("MAX_REPAIR_ATTEMPTS must be between 0 and 2")
        for key in ("llm_timeout_seconds", "llm_context_tokens", "llm_max_output_tokens", "max_upload_mb", "sandbox_cpus", "sandbox_timeout_seconds"):
            if getattr(self, key) <= 0:
                raise ValueError(f"{key} must be positive")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(Path.cwd() / ".env", override=False)
        if os.getenv("LLM_PROVIDER", "ollama") != "ollama":
            raise ValueError("Only the local Ollama adapter is configured")
        defaults = cls()
        values = {}
        for field in fields(cls):
            raw = os.getenv(field.name.upper())
            if raw is not None:
                current = getattr(defaults, field.name)
                values[field.name] = Path(raw) if isinstance(current, Path) else type(current)(raw)
        return cls(**values)
