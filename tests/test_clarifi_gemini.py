import json
import pytest

pytest.importorskip("google.genai")
pytest.importorskip("sklearn")

from workbench.agents.clarifi.models import GeminiAPIReasoner, RulesReasoner


def test_api_request_uses_stateless_interactions_and_key_header(monkeypatch):
    import httpx
    captured = {}
    def respond(request):
        captured.update(json.loads(request.content))
        captured["url"] = str(request.url)
        captured["key"] = request.headers["x-goog-api-key"]
        return httpx.Response(200, json={"status":"completed","steps":[{"type":"model_output","content":[{"type":"text","text":'{"tools":["shell_companies"],"rationale":"Review supplied payments"}'}]}]})
    client_class = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr("workbench.agents.clarifi.models.gemini_api_config", lambda: {"key": "fixture-secret", "model": "test-model"})
    reasoner = GeminiAPIReasoner()
    assert reasoner.plan({"available_tools": ["shell_companies"]}).tools == ["shell_companies"]
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert captured["key"] == "fixture-secret"
    assert "fixture-secret" not in captured["url"]
    assert captured["model"] == "test-model"
    assert captured["store"] is False
    assert captured["response_format"]["schema"]["additionalProperties"] is False
    assert captured["response_format"]["schema"]["properties"]["tools"]["items"]["enum"] == ["shell_companies"]


@pytest.mark.parametrize("model", ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemma-4-26b-a4b-it", "gemma-4-31b-it"])
def test_model_specific_settings_keep_structured_validation(monkeypatch, model):
    from workbench.agents.clarifi.models import API_MODELS
    assert model in API_MODELS
    monkeypatch.setattr("workbench.agents.clarifi.models.gemini_api_config", lambda: {"key":"fixture-secret","model":model})
    packets = []
    def interaction(self, body):
        packets.append(body)
        return {"status":"completed","output_text":'{"tools":["shell_companies"],"rationale":"Review payments"}'}
    monkeypatch.setattr(GeminiAPIReasoner,"_interaction",interaction)
    assert GeminiAPIReasoner().plan({"available_tools":["shell_companies"]}).tools == ["shell_companies"]
    packet = packets[0]
    assert packet["model"] == model and packet["store"] is False
    assert packet["response_format"]["mime_type"] == "application/json"
    assert ("thinking_level" in packet["generation_config"]) == model.startswith("gemini-3")


def test_quota_rejection_is_returned_after_one_request_without_fallback(monkeypatch):
    import httpx
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(429, json={"error":{"message":"fixture-secret: daily request limit reached"}})
    client_class = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr("workbench.agents.clarifi.models.gemini_api_config", lambda: {"key":"fixture-secret","model":"test-model"})
    with pytest.raises(RuntimeError, match="HTTP 429") as error:
        GeminiAPIReasoner().plan({"available_tools":["shell_companies"]})
    assert "quota" in str(error.value) and "fixture-secret" not in str(error.value)
    assert len(requests) == 1


def test_health_never_echoes_raw_sdk_errors(monkeypatch):
    monkeypatch.setattr("workbench.agents.clarifi.models.gemini_api_config", lambda: {"key": "fixture-secret", "model": "test-model"})
    def fail(self):
        raise RuntimeError("request included fixture-secret")
    monkeypatch.setattr(GeminiAPIReasoner, "_client", fail)
    result = GeminiAPIReasoner().health()
    assert not result["available"]
    assert "fixture-secret" not in json.dumps(result)


@pytest.mark.parametrize("code,fragment", [(401,"API key"),(403,"restrictions"),(429,"quota"),(400,"request format"),(404,"model ID"),(503,"service")])
def test_request_errors_distinguish_http_failures_without_echoing_secrets(code, fragment):
    from workbench.agents.clarifi.models import gemini_request_error
    error = RuntimeError("fixture-secret and private source records")
    error.status_code = code
    message = gemini_request_error(error)
    assert f"HTTP {code}" in message
    assert fragment in message
    assert "fixture-secret" not in message and "private source" not in message


def test_timeout_and_connectivity_are_not_reported_as_authentication_failures():
    import httpx
    from workbench.agents.clarifi.models import gemini_request_error
    timeout = httpx.ReadTimeout("request URL contains fixture-secret")
    wrapper = RuntimeError("SDK wrapper")
    wrapper.__cause__ = timeout
    assert "timed out after 90 seconds" in gemini_request_error(wrapper)
    assert "HTTP unavailable" not in gemini_request_error(wrapper)
    assert "fixture-secret" not in gemini_request_error(wrapper)
    assert "could not be reached" in gemini_request_error(httpx.ConnectError("fixture-secret"))


def test_missing_key_does_not_fall_back(monkeypatch):
    monkeypatch.setattr("workbench.agents.clarifi.models.gemini_api_config", lambda: {"key": "", "model": "test-model"})
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiAPIReasoner()


def test_narrative_schema_is_portable_but_validation_stays_strict():
    from workbench.agents.clarifi.models import gemini_json_schema
    from workbench.agents.clarifi.schemas import Narrative
    from pydantic import ValidationError
    schema = gemini_json_schema(Narrative.model_json_schema())
    assert "$ref" not in json.dumps(schema)
    assert schema["properties"]["claims"]["items"]["required"] == ["finding_id", "explanation", "evidence_ids"]
    with pytest.raises(ValidationError):
        Narrative.model_validate({"claims":[{"finding_id":"F1","explanation":"x"*1501,"evidence_ids":["source:1"]}],"next_steps":[]})


def test_gemini_provider_provenance_contains_no_credentials(monkeypatch):
    from workbench.agents.clarifi.agent import ClariFiAgent
    from workbench.agents.clarifi.ingest import sample_input
    class FakeAPI(RulesReasoner):
        provider = "gemini_api"
        model = "test-gemini"
        def __init__(self, model=None):
            self._api_key = "fixture-secret"
    monkeypatch.setattr("workbench.agents.clarifi.agent.GeminiAPIReasoner", FakeAPI)
    report = ClariFiAgent(mode="gemini").run(sample_input("shell_companies"), approved=True)
    assert report["provenance"]["ai_used"]
    assert report["provenance"]["reasoning_provider"] == "gemini_api"
    assert "fixture-secret" not in json.dumps(report)


def test_financial_mapping_decoder_relaxes_array_bounds_but_local_validation_keeps_them(monkeypatch):
    from pydantic import Field, ValidationError, create_model
    schema = create_model('FinancialMappingPlan', items=(list[str], Field(min_length=1, max_length=2)))
    monkeypatch.setattr('workbench.agents.clarifi.models.gemini_api_config', lambda: {'key': 'fixture-secret', 'model': 'fixture-gemini'})
    captured = []
    def interaction(self, body):
        captured.append(body)
        return {'status': 'completed', 'output_text': '{"items":["a","b","c"]}'}
    monkeypatch.setattr(GeminiAPIReasoner, '_interaction', interaction)
    with pytest.raises(ValidationError):
        GeminiAPIReasoner()._generate(schema, 'Use only reviewed mappings', {})
    items = captured[0]['response_format']['schema']['properties']['items']
    assert 'maxItems' not in items and 'minItems' not in items
    assert schema.model_json_schema()['properties']['items']['maxItems'] == 2
