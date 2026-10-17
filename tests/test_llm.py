"""Provider tests use explicit HTTP fixtures; no production fallback is exercised."""
import json

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from workbench.llm import LLMError, OllamaClient, OpenAICompatibleClient, create_llm
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

    settings = Settings(workspace_root=tmp_path, ollama_model="llama3.1:8b")
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


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
def test_full_schema_is_prompted_once_and_preserved_during_repairs(tmp_path, mode):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        content = '{"wrong":1}' if len(requests) == 1 else '{"value":7}'
        return httpx.Response(200, json={"message": {"content": content}})

    client = OllamaClient(Settings(workspace_root=tmp_path, ollama_model="gemma-fixture:4b", llm_structured_mode=mode),
                          client=httpx.Client(transport=httpx.MockTransport(respond)))
    assert client.structured(Answer, "Return a count", "Calculate it").value == 7
    assert len(requests) == 2
    schema_text = json.dumps(Answer.model_json_schema(), separators=(",", ":"))
    for request in requests:
        system = request["messages"][0]["content"]
        assert system.count("Required JSON schema:") == 1
        assert system.count(schema_text) == 1
        assert request["format"] == (Answer.model_json_schema() if mode == "json_schema" else "json")
    assert requests[0]["messages"][0] == requests[1]["messages"][0]
    assert '"path": "value"' in requests[1]["messages"][1]["content"]


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


def compatible_provider(tmp_path, responses, *, status=200, **settings_overrides):
    requests = []

    def handle(request):
        requests.append(request)
        body = responses[min(len(requests) - 1, len(responses) - 1)]
        return httpx.Response(status, json=body)

    settings = Settings(workspace_root=tmp_path, llm_provider="openai_compatible",
                        llm_model="local-owner/gemma-exact-id:q4", **settings_overrides)
    client = create_llm(settings, client=httpx.Client(transport=httpx.MockTransport(handle)))
    return client, requests


def completion(content='{"value": 7}', finish_reason="stop"):
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}]}


def test_factory_keeps_ollama_default(tmp_path):
    client = create_llm(Settings(workspace_root=tmp_path))
    try:
        assert isinstance(client, OllamaClient)
        assert client.model == "auto"
    finally:
        client.close()


def test_compatible_schema_payload_uses_exact_model_and_optional_auth(tmp_path):
    client, requests = compatible_provider(tmp_path, [completion()], llm_api_key="fixture-local-secret")
    assert isinstance(client, OpenAICompatibleClient)
    assert client.structured(Answer, "Return the measured value", "Measure").value == 7
    request = requests[0]
    assert str(request.url) == "http://localhost:1234/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer fixture-local-secret"
    payload = json.loads(request.content)
    assert payload["model"] == "local-owner/gemma-exact-id:q4"
    assert payload["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "Answer", "strict": True, "schema": Answer.model_json_schema(),
    }}
    assert payload["max_tokens"] == 4096
    assert payload["stream"] is False


def test_compatible_unauthenticated_server_receives_no_dummy_key(tmp_path):
    client, requests = compatible_provider(tmp_path, [completion()], llm_api_key="")
    client.structured(Answer, "Return value", "Go")
    assert "Authorization" not in requests[0].headers


def test_explicit_json_object_mode_supplies_schema_and_repairs_invalid_answers(tmp_path):
    client, requests = compatible_provider(tmp_path, [completion('{"unapproved": 1}'), completion()],
                                           llm_structured_mode="json_object")
    assert client.structured(Answer, "Return value", "Go").value == 7
    assert len(requests) == 2
    for request in requests:
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert "Required JSON schema:" in payload["messages"][0]["content"]
        assert '"value"' in payload["messages"][0]["content"]


def test_unsupported_schema_mode_has_clear_error_without_silent_fallback(tmp_path):
    client, requests = compatible_provider(tmp_path, [{"error": "JSON Schema is unsupported"}], status=400)
    with pytest.raises(LLMError, match="explicitly set LLM_STRUCTURED_MODE=json_object"):
        client.structured(Answer, "Return value", "Go")
    assert len(requests) == 1
    assert json.loads(requests[0].content)["response_format"]["type"] == "json_schema"


def test_compatible_model_health_requires_exact_id_and_preserves_inventory(tmp_path):
    client, requests = compatible_provider(tmp_path, [{"data": [{"id": "local-owner/gemma-exact-id:q4"}, {"id": "other-model"}]}])
    health = client.health()
    assert health["available"] and health["model_available"]
    assert health["provider"] == "openai_compatible"
    assert health["model"] == "local-owner/gemma-exact-id:q4"
    assert health["models"] == ["local-owner/gemma-exact-id:q4", "other-model"]
    assert str(requests[0].url).endswith("/v1/models")
    assert requests[0].extensions["timeout"]["read"] == 10.0

    mismatch, _ = compatible_provider(tmp_path, [{"data": [{"id": "gemma-exact-id:q4"}]}])
    assert mismatch.health()["model_available"] is False


def test_compatible_length_stop_is_not_accepted_as_complete(tmp_path):
    client, requests = compatible_provider(tmp_path, [completion(finish_reason="length")])
    with pytest.raises(LLMError, match="Output token limit reached"):
        client.structured(Answer, "Return value", "Go")
    assert len(requests) == 3


def test_compatible_key_is_redacted_from_errors_and_tracebacks(tmp_path):
    import traceback
    secret = "fixture-local-secret-token"
    client, _ = compatible_provider(tmp_path, [{"error": "Rejected credential " + secret}], status=401, llm_api_key=secret)
    with pytest.raises(LLMError, match="LLM_API_KEY") as raised:
        client.structured(Answer, "Return value", "Go")
    formatted = "".join(traceback.format_exception(raised.value))
    assert secret not in formatted
    assert "[redacted]" in formatted
    assert secret not in json.dumps(client.health())
    assert secret not in repr(client.settings)


def test_compatible_transport_errors_do_not_echo_credentials(tmp_path):
    import traceback
    secret = "fixture-local-key"

    def fail(request):
        raise httpx.ConnectError("Sensitive transport detail " + secret, request=request)

    settings = Settings(workspace_root=tmp_path, llm_provider="openai_compatible", llm_model="fixture-model", llm_api_key=secret)
    client = create_llm(settings, client=httpx.Client(transport=httpx.MockTransport(fail)))
    with pytest.raises(LLMError, match="Cannot reach") as raised:
        client.structured(Answer, "Return value", "Go")
    assert secret not in "".join(traceback.format_exception(raised.value))


def test_redirects_are_never_followed_even_with_authenticated_client(tmp_path):
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://public.example/collect"})

    settings = Settings(workspace_root=tmp_path, llm_provider="openai_compatible", llm_model="fixture-model", llm_api_key="local-secret")
    client = create_llm(settings, client=httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True))
    with pytest.raises(LLMError, match="redirect"):
        client.structured(Answer, "Return value", "Go")
    assert len(requests) == 1
    assert requests[0].url.host == "localhost"


def test_compatible_timeout_and_malformed_api_fail_truthfully(tmp_path):
    def timeout(request):
        raise httpx.ReadTimeout("Fixture timeout", request=request)

    settings = Settings(workspace_root=tmp_path, llm_provider="openai_compatible", llm_model="fixture-model")
    client = create_llm(settings, client=httpx.Client(transport=httpx.MockTransport(timeout)))
    assert "timed out after 10.0s" in client.health()["error"]
    malformed, _ = compatible_provider(tmp_path, [{"choices": []}])
    with pytest.raises(LLMError, match="no chat completion choices"):
        malformed.structured(Answer, "Return value", "Go")


def test_compatible_structured_retry_budget_and_context_limit(tmp_path):
    client, requests = compatible_provider(tmp_path, [completion("invalid JSON")])
    with pytest.raises(LLMError, match="after 3 attempts"):
        client.structured(Answer, "Return value", "Go")
    assert len(requests) == 3
    with pytest.raises(LLMError, match="silently truncated"):
        client.structured(Answer, "Return value", "x" * 100_000)
    assert len(requests) == 3


def test_context_budget_reserves_the_full_configured_output(tmp_path):
    client, requests = compatible_provider(tmp_path, [completion()], llm_context_tokens=1024, llm_max_output_tokens=768)
    with pytest.raises(LLMError, match="configured context"):
        client.structured(Answer, "Return value", "x" * 1200)
    assert requests == []
