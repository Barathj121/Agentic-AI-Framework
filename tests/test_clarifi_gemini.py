import json
from types import SimpleNamespace

import pytest

pytest.importorskip("google.genai")
pytest.importorskip("sklearn")

from workbench.agents.clarifi.models import GeminiAPIReasoner, RulesReasoner


def test_api_key_is_explicit_and_sdk_does_not_use_vertex(monkeypatch):
    from google import genai
    from workbench.agents.clarifi.schemas import Plan
    captured = {}
    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.interactions = self
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(status="completed", output_text='{"tools":["shell_companies"],"rationale":"Review supplied payments"}')
    monkeypatch.setattr(genai, "Client", Client)
    monkeypatch.setattr("workbench.agents.clarifi.models.gemini_api_config", lambda: {"key": "fixture-secret", "model": "test-model"})
    reasoner = GeminiAPIReasoner()
    assert reasoner.plan({"available_tools": ["shell_companies"]}).tools == ["shell_companies"]
    assert captured["vertexai"] is False
    assert captured["api_key"] == "fixture-secret"
    assert captured["model"] == "test-model"
    assert captured["store"] is False
    assert captured["response_format"]["schema"]["additionalProperties"] is False
    assert captured["response_format"]["schema"]["properties"]["tools"]["items"]["enum"] == ["shell_companies"]


def test_health_never_echoes_raw_sdk_errors(monkeypatch):
    monkeypatch.setattr("workbench.agents.clarifi.models.gemini_api_config", lambda: {"key": "fixture-secret", "model": "test-model"})
    def fail(self):
        raise RuntimeError("request included fixture-secret")
    monkeypatch.setattr(GeminiAPIReasoner, "_client", fail)
    result = GeminiAPIReasoner().health()
    assert not result["available"]
    assert "fixture-secret" not in json.dumps(result)


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
