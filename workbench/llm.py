"""Small local Ollama adapter with bounded, validated structured responses."""

from __future__ import annotations

import json
import re
from typing import TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from workbench.settings import Settings

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """A local provider or model capability failure, never a fabricated answer."""


class OllamaClient:
    """Use only the configured Ollama endpoint; never select a cloud fallback.

    Ambient proxy variables are deliberately ignored for the local HTTP client.
    This does not modify Windows settings or Ollama's model-download settings.
    """

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None):
        self.settings = settings
        parsed = urlsplit(settings.ollama_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("OLLAMA_BASE_URL must be an HTTP(S) server URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("OLLAMA_BASE_URL cannot contain credentials, a query, or fragment")
        self.base_url = settings.ollama_base_url.rstrip("/")
        self._client = client or httpx.Client(
            trust_env=False,
            timeout=httpx.Timeout(float(settings.llm_timeout_seconds), connect=10.0),
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, endpoint: str, payload: dict | None = None, *, timeout: float | None = None) -> dict:
        try:
            options = {"timeout": timeout} if timeout is not None else {}
            with self._client.stream(method, self.base_url + endpoint, json=payload, **options) as response:
                parts, size = [], 0
                for part in response.iter_bytes():
                    size += len(part)
                    if size > 2_000_000:
                        raise LLMError("Ollama response exceeded the 2 MB response limit")
                    parts.append(part)
                body = b"".join(parts).decode("utf-8", errors="replace")
                if response.status_code >= 400:
                    if response.status_code == 404:
                        raise LLMError(
                            f"Ollama model or endpoint is unavailable. Confirm the model with "
                            f"'ollama list' and pull {self.settings.ollama_model!r}. "
                            f"Server response: {body[:600]}"
                        )
                    raise LLMError(f"Ollama HTTP {response.status_code}: {body[:600]}")
                parsed = json.loads(body)
                if not isinstance(parsed, dict):
                    raise LLMError("Ollama returned a non-object API response")
                if parsed.get("error"):
                    raise LLMError(f"Ollama error: {str(parsed['error'])[:600]}")
                return parsed
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"Ollama request timed out after {timeout or self.settings.llm_timeout_seconds}s. "
                "Try a smaller local model or increase LLM_TIMEOUT_SECONDS."
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(
                f"Cannot reach Ollama at {self.base_url}. Start Ollama and verify its endpoint. "
                f"{type(exc).__name__}: {str(exc)[:300]}"
            ) from exc
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise LLMError("Ollama returned an invalid JSON API envelope") from exc

    def health(self) -> dict:
        try:
            response = self._request("GET", "/api/tags", timeout=10.0)
            models = [m.get("name", m.get("model", "")) for m in response.get("models", [])]
            name = self.settings.ollama_model
            names = {name, name + ":latest"} if ":" not in name else {name}
            available = bool(names.intersection(models))
            return {
                "available": True,
                "model_available": available,
                "model": name,
                "models": models,
                "error": None if available else f"Download the configured model: ollama pull {name}",
            }
        except LLMError as exc:
            return {"available": False, "model_available": False, "error": str(exc)}

    def structured(self, schema_class: type[T], system: str, prompt: str) -> T:
        """Validate every answer, with at most two schema-repair requests.

        Character budgeting is conservative guidance, not a tokenizer claim. We
        reject oversized contracts instead of silently truncating approved goals.
        Ollama also enforces num_ctx and num_predict at generation time.
        """
        schema = schema_class.model_json_schema()
        schema_text = json.dumps(schema, separators=(",", ":"))
        system = system + "\nReturn only one complete JSON object matching the supplied schema."
        reserved_output = min(self.settings.llm_max_output_tokens, self.settings.llm_context_tokens // 2)
        input_budget = max(1024, (self.settings.llm_context_tokens - reserved_output) * 4)
        initial_size = len(system) + len(prompt) + len(schema_text)
        if initial_size > input_budget:
            raise LLMError(
                f"The task needs about {initial_size} input characters; the configured context "
                f"budget permits {input_budget}. Increase LLM_CONTEXT_TOKENS or simplify the plan. "
                "No approved requirements were silently truncated."
            )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        last_error = ""
        for attempt in range(3):
            payload = {
                "model": self.settings.ollama_model,
                "messages": messages,
                "stream": False,
                "format": schema,
                "options": {
                    "temperature": self.settings.llm_temperature,
                    "num_ctx": self.settings.llm_context_tokens,
                    "num_predict": self.settings.llm_max_output_tokens,
                },
            }
            response = self._request("POST", "/api/chat", payload)
            message = response.get("message", {})
            raw = message.get("content", "") if isinstance(message, dict) else ""
            if not isinstance(raw, str):
                raw = ""
            cleaned = raw.strip()
            fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
            if fenced:
                cleaned = fenced.group(1)
            try:
                return schema_class.model_validate_json(cleaned)
            except ValidationError as exc:
                # Do not include raw input values or large source files in errors.
                errors = exc.errors(include_input=False, include_url=False)
                last_error = json.dumps(errors, default=str)[:1000]
                if response.get("done_reason") == "length":
                    last_error = "Output token limit reached. " + last_error
                if attempt < 2:
                    repair = (
                        "\nYour previous response failed schema validation. Return the full corrected "
                        "object, preserving the requested objectives. Validation errors: " + last_error
                    )
                    remaining = input_budget - initial_size
                    if remaining < 200:
                        raise LLMError(
                            "The model returned invalid structured output and no context budget remains "
                            "for a repair. Increase LLM_CONTEXT_TOKENS. " + last_error
                        ) from exc
                    messages = [messages[0], {"role": "user", "content": prompt + repair[:remaining]}]
        raise LLMError(
            "The configured local model could not produce valid structured output after 3 attempts. "
            "Try a more capable local model or a larger context/output budget. " + last_error
        )
