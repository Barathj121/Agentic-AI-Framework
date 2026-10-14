"""Local model adapters sharing bounded requests and Pydantic schema validation."""

from __future__ import annotations

import json
import re
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from workbench.settings import Settings

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """A local provider or model capability failure, never a fabricated answer."""


class _LocalStructuredClient:
    """Use only the explicitly configured local provider; never fall back.

    Ambient proxy variables are deliberately ignored for the local HTTP client.
    This does not modify Windows settings or Ollama's model-download settings.
    """

    def __init__(self, settings: Settings, *, base_url: str, model: str, provider: str,
                 label: str, api_key: str = "", client: httpx.Client | None = None):
        self.settings = settings
        self.base_url = base_url
        self.model = model
        self.provider = provider
        self.label = label
        self._api_key = api_key
        self._client = client or httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(float(settings.llm_timeout_seconds), connect=10.0),
        )

    def close(self) -> None:
        self._client.close()

    def _redact(self, value: str) -> str:
        secret = self.settings.llm_api_key
        return value.replace(secret, "[redacted]") if secret else value

    def _request(self, method: str, endpoint: str, payload: dict | None = None, *, timeout: float | None = None) -> dict:
        try:
            options = {"timeout": timeout} if timeout is not None else {}
            headers = {"Authorization": "Bearer " + self._api_key} if self._api_key else {}
            with self._client.stream(method, self.base_url + endpoint, json=payload, headers=headers,
                                     follow_redirects=False, **options) as response:
                parts, size = [], 0
                for part in response.iter_bytes():
                    size += len(part)
                    if size > 2_000_000:
                        raise LLMError(f"{self.label} response exceeded the 2 MB response limit")
                    parts.append(part)
                body = b"".join(parts).decode("utf-8", errors="replace")
                if 300 <= response.status_code < 400:
                    raise LLMError(f"{self.label} returned a redirect, which is disabled. Configure the direct local API endpoint.")
                if response.status_code >= 400:
                    detail = self._redact(body)[:600]
                    if response.status_code == 404:
                        raise LLMError(
                            f"{self.label} model or endpoint is unavailable. {self._missing_model()} "
                            f"Server response: {detail}"
                        )
                    guidance = ""
                    if response.status_code in {400, 422} and self.settings.llm_structured_mode == "json_schema":
                        guidance = " If this server does not support JSON Schema, explicitly set LLM_STRUCTURED_MODE=json_object. No automatic fallback was attempted."
                    if response.status_code in {401, 403}:
                        guidance = " Check the local server's authentication setting and LLM_API_KEY."
                    raise LLMError(f"{self.label} HTTP {response.status_code}: {detail}{guidance}")
                parsed = json.loads(body)
                if not isinstance(parsed, dict):
                    raise LLMError(f"{self.label} returned a non-object API response")
                if parsed.get("error"):
                    raise LLMError(f"{self.label} error: {self._redact(str(parsed['error']))[:600]}")
                return parsed
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"{self.label} request timed out after {timeout or self.settings.llm_timeout_seconds}s. "
                "Try a smaller local model or increase LLM_TIMEOUT_SECONDS."
            ) from None
        except httpx.HTTPError as exc:
            raise LLMError(
                f"Cannot reach {self.label} at {self.base_url}. Start the local model server and verify its endpoint. "
                f"Transport failure: {type(exc).__name__}."
            ) from None
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise LLMError(f"{self.label} returned an invalid JSON API envelope") from None

    def _models(self) -> list[str]:
        raise NotImplementedError

    def _model_is_available(self, models: list[str]) -> bool:
        return self.model in models

    def _missing_model(self) -> str:
        return "Load the intended local model and set LLM_MODEL to its exact ID from GET /models."

    def health(self) -> dict:
        try:
            models = self._models()
            available = self._model_is_available(models)
            return {
                "available": True,
                "model_available": available,
                "provider": self.provider,
                "model": self.model,
                "base_url": self.base_url,
                "structured_mode": self.settings.llm_structured_mode,
                "models": models,
                "error": None if available else self._missing_model(),
            }
        except LLMError as exc:
            return {"available": False, "model_available": False, "provider": self.provider,
                    "model": self.model, "base_url": self.base_url, "error": self._redact(str(exc))}

    def _generate(self, schema_name: str, schema: dict, messages: list[dict]) -> tuple[str, str | None]:
        raise NotImplementedError

    def structured(self, schema_class: type[T], system: str, prompt: str) -> T:
        """Validate every answer, with at most two schema-repair requests.

        Character budgeting is conservative guidance, not a tokenizer claim. We
        reject oversized contracts instead of silently truncating approved goals.
        Ollama enforces num_ctx/num_predict. Compatible endpoints receive
        max_tokens; their loaded model context must also be configured locally.
        """
        schema = schema_class.model_json_schema()
        schema_text = json.dumps(schema, separators=(",", ":"))
        system = system + "\nReturn only one complete JSON object matching the supplied schema."
        if self.settings.llm_structured_mode == "json_object":
            system += "\nRequired JSON schema:\n" + schema_text
        reserved_output = self.settings.llm_max_output_tokens
        input_budget = (self.settings.llm_context_tokens - reserved_output) * 4
        initial_size = len(system) + len(prompt) + (len(schema_text) if self.settings.llm_structured_mode == "json_schema" else 0)
        if initial_size > input_budget:
            raise LLMError(
                f"The task needs about {initial_size} input characters; the configured context "
                f"budget permits {input_budget}. Increase LLM_CONTEXT_TOKENS or simplify the plan. "
                "No approved requirements were silently truncated."
            )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        last_error = ""
        for attempt in range(3):
            raw, finish_reason = self._generate(schema_class.__name__, schema, messages)
            if not isinstance(raw, str):
                raw = ""
            cleaned = raw.strip()
            fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
            if fenced:
                cleaned = fenced.group(1)
            try:
                value = schema_class.model_validate_json(cleaned)
            except ValidationError as exc:
                # Do not include raw input values or large source files in errors.
                errors = exc.errors(include_input=False, include_url=False)
                last_error = self._redact(json.dumps(errors, default=str))[:1000]
            else:
                if finish_reason != "length":
                    return value
                last_error = "The server stopped at the output token limit, so the response cannot be considered complete."
            if finish_reason == "length":
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
                    ) from None
                messages = [messages[0], {"role": "user", "content": prompt + repair[:remaining]}]
        raise LLMError(
            "The configured local model could not produce valid structured output after 3 attempts. "
            "Try a more capable local model or a larger context/output budget. " + last_error
        )


class OllamaClient(_LocalStructuredClient):
    def __init__(self, settings: Settings, *, client: httpx.Client | None = None):
        super().__init__(settings, base_url=settings.ollama_base_url, model=settings.ollama_model,
                         provider="ollama", label="Ollama", client=client)

    def _missing_model(self) -> str:
        return f"Confirm the model with 'ollama list' and download it with: ollama pull {self.model}"

    def _models(self) -> list[str]:
        response = self._request("GET", "/api/tags", timeout=10.0)
        models = response.get("models")
        if not isinstance(models, list) or any(not isinstance(model, dict) for model in models):
            raise LLMError("Ollama returned an invalid model list")
        return [model.get("name", model.get("model", "")) for model in models
                if isinstance(model.get("name", model.get("model", "")), str)]

    def _model_is_available(self, models: list[str]) -> bool:
        names = {self.model, self.model + ":latest"} if ":" not in self.model else {self.model}
        return bool(names.intersection(models))

    def _generate(self, schema_name: str, schema: dict, messages: list[dict]) -> tuple[str, str | None]:
        response = self._request("POST", "/api/chat", {
            "model": self.model, "messages": messages, "stream": False,
            "format": schema if self.settings.llm_structured_mode == "json_schema" else "json",
            "options": {"temperature": self.settings.llm_temperature,
                        "num_ctx": self.settings.llm_context_tokens,
                        "num_predict": self.settings.llm_max_output_tokens},
        })
        message = response.get("message", {})
        return (message.get("content", "") if isinstance(message, dict) else "", response.get("done_reason"))


class OpenAICompatibleClient(_LocalStructuredClient):
    """LM Studio or another explicitly configured local compatible API server."""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None):
        if not settings.llm_model.strip():
            raise ValueError("LLM_MODEL must be the exact model ID returned by the local server's GET /models")
        super().__init__(settings, base_url=settings.llm_base_url, model=settings.llm_model,
                         provider="openai_compatible", label="Local OpenAI-compatible server",
                         api_key=settings.llm_api_key, client=client)

    def _models(self) -> list[str]:
        response = self._request("GET", "/models", timeout=10.0)
        models = response.get("data")
        if not isinstance(models, list) or any(not isinstance(model, dict) or not isinstance(model.get("id"), str) for model in models):
            raise LLMError("Local compatible server returned an invalid model list; GET /models must return data with model IDs")
        return [model["id"] for model in models]

    def _generate(self, schema_name: str, schema: dict, messages: list[dict]) -> tuple[str, str | None]:
        response_format = {"type": "json_object"}
        if self.settings.llm_structured_mode == "json_schema":
            name = re.sub(r"[^A-Za-z0-9_-]", "_", schema_name)[:64] or "response"
            response_format = {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}
        response = self._request("POST", "/chat/completions", {
            "model": self.model, "messages": messages, "stream": False,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_output_tokens,
            "response_format": response_format,
        })
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LLMError("Local compatible server returned no chat completion choices")
        message = choices[0].get("message", {})
        if isinstance(message, dict) and message.get("refusal"):
            raise LLMError("The local model declined this request; no structured result was produced")
        return (message.get("content", "") if isinstance(message, dict) else "", choices[0].get("finish_reason"))


def create_llm(settings: Settings, *, client: httpx.Client | None = None) -> OllamaClient | OpenAICompatibleClient:
    """Select exactly one provider from configuration; never route by availability."""
    if settings.llm_provider == "ollama":
        return OllamaClient(settings, client=client)
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleClient(settings, client=client)
    raise ValueError("LLM_PROVIDER must be ollama or openai_compatible")
