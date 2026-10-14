"""Provider tests use explicit HTTP fixtures; no production fallback is exercised."""
import json

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from workbench.llm import LLMError, OllamaClient
from workbench.settings import Settings


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


def provider(tmp_path, bodies, status=200):
    requests = []

    def handle(request):
        requests.append(request)
        body = bodies[min(len(requests) - 1, len(bodies) - 1)]
        return httpx.Response(status, json=body)

    settings = Settings(workspace_root=tmp_path)
    client = OllamaClient(settings, client=httpx.Client(transport=httpx.MockTransport(handle)))
    return client, requests


def test_invalid_json_and_schema_repaired_with_bounded_retries(tmp_path):
    client, requests = provider(tmp_path, [
        {"message": {"content": "not json"}},
        {"message": {"content": '{"wrong": 1}'}},
        {"message": {"content": '{"value": 9}'}},
    ])
    assert client.structured(Answer, "Return a count", "Calculate it").value == 9
    assert len(requests) == 3
    payload = json.loads(requests[-1].content)
    assert payload["format"]["properties"]["value"]["type"] == "integer"
    assert payload["stream"] is False
    assert "Validation errors" in payload["messages"][1]["content"]
    assert payload["model"] == "llama3.1:8b"


def test_fenced_json_is_validated(tmp_path):
    client, _ = provider(tmp_path, [{"message": {"content": '```json\n{"value": 5}\n```'}}])
    assert client.structured(Answer, "Return count", "Go").value == 5


def test_invalid_answers_fail_without_fabricated_result(tmp_path):
    client, requests = provider(tmp_path, [{"message": {"content": '{"value": "made up"}'}}])
    with pytest.raises(LLMError, match="after 3 attempts"):
        client.structured(Answer, "Count", "Go")
    assert len(requests) == 3


def test_health_distinguishes_server_and_model_availability(tmp_path):
    client, _ = provider(tmp_path, [{"models": [{"name": "other:latest"}]}])
    health = client.health()
    assert health["available"] is True
    assert health["model_available"] is False
    assert "ollama pull" in health["error"]


def test_missing_model_has_actionable_error_and_no_retry(tmp_path):
    client, requests = provider(tmp_path, [{"error": "model not found"}], status=404)
    with pytest.raises(LLMError, match="ollama list"):
        client.structured(Answer, "Count", "Go")
    assert len(requests) == 1


def test_timeout_is_truthful(tmp_path):
    def timeout(request):
        raise httpx.ReadTimeout("Fixture timeout", request=request)

    client = OllamaClient(Settings(workspace_root=tmp_path), client=httpx.Client(transport=httpx.MockTransport(timeout)))
    with pytest.raises(LLMError, match="timed out"):
        client.structured(Answer, "Count", "Go")


def test_context_contract_is_never_silently_truncated(tmp_path):
    client, requests = provider(tmp_path, [{"message": {"content": '{"value": 1}'}}])
    with pytest.raises(LLMError, match="silently truncated"):
        client.structured(Answer, "Count", "x" * 100_000)
    assert not requests


def test_response_envelope_size_is_bounded(tmp_path):
    client, _ = provider(tmp_path, [{"message": {"content": "x" * 2_000_001}}])
    with pytest.raises(LLMError, match="2 MB"):
        client.structured(Answer, "Count", "Go")


def test_no_credentials_in_provider_endpoint(tmp_path):
    with pytest.raises(ValueError, match="credentials"):
        OllamaClient(Settings(workspace_root=tmp_path, ollama_base_url="http://user:password@localhost:11434"))
