import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="Install requirements-clarifi.txt for optional API tests")
pytest.importorskip("sklearn")

from fastapi.testclient import TestClient
from workbench.agents.clarifi.api import app


@pytest.fixture
def request_body(monkeypatch):
    monkeypatch.setenv("CLARIFI_MODE", "rules")
    monkeypatch.delenv("CLARIFI_EVIDENCE_BUCKET", raising=False)
    return {"approved": True, "data": json.loads((Path(__file__).resolve().parents[1] / "examples/clarifi/financial_case.json").read_text())}


def test_registry_schema_and_both_financial_capabilities(request_body):
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert "clarifi" in client.get("/agents").json()
        assert "data" in client.get("/input-schema").json()["properties"]
        result = client.post("/investigations", json=request_body)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "COMPLETED"
    assert not result.json()["provenance"]["ai_used"]
    assert result.json()["evidence_uri"] is None


def test_api_approval_validation_and_size_limit(request_body):
    with TestClient(app) as client:
        request_body["approved"] = False
        assert client.post("/investigations", json=request_body).status_code == 409
        request_body["approved"] = "true"
        assert client.post("/investigations", json=request_body).status_code == 422
        assert client.post("/investigations", content=b"x" * (5*1024*1024 + 1)).status_code == 413
        response = client.post("/investigations", json={"secret_name": "Sensitive Person"})
        assert response.status_code == 422
        assert "Sensitive Person" not in response.text


def test_cloud_failure_is_not_replaced_by_rules(request_body, monkeypatch):
    monkeypatch.setenv("CLARIFI_MODE", "vertex")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("CLARIFI_GEMINI_MODEL", raising=False)
    with TestClient(app) as client:
        result = client.post("/investigations", json=request_body)
    assert result.status_code == 502
    assert "findings" not in result.json()


def test_storage_failure_does_not_report_success(request_body, monkeypatch):
    def fail(*args):
        raise RuntimeError("sensitive internal storage configuration")
    monkeypatch.setattr("workbench.agents.clarifi.api.persist", fail)
    with TestClient(app) as client:
        result = client.post("/investigations", json=request_body)
    assert result.status_code == 502
    assert "sensitive internal" not in result.text


def test_evidence_bundle_contains_sources_and_uses_create_only_storage(request_body, monkeypatch):
    pytest.importorskip("google.cloud.storage")
    from google.cloud import storage
    from workbench.agents.clarifi.schemas import InvestigationInput
    from workbench.agents.clarifi.storage import persist
    captured = {}
    class FakeStorage:
        def bucket(self, name):
            captured["bucket"] = name
            return self
        def blob(self, name):
            captured["object"] = name
            return self
        def upload_from_string(self, content, **kwargs):
            captured.update(kwargs)
            captured["bundle"] = json.loads(content)
    monkeypatch.setenv("CLARIFI_EVIDENCE_BUCKET", "private-test-evidence")
    monkeypatch.setattr(storage, "Client", lambda: FakeStorage())
    source = InvestigationInput.model_validate(request_body["data"])
    report = {"case_id": source.case_id, "investigation_id": "run-123", "status": "COMPLETED"}
    uri = persist(source, report)
    assert captured["if_generation_match"] == 0
    assert captured["bundle"]["input"] == source.model_dump(mode="json")
    assert captured["bundle"]["report"] == report
    assert uri == "gs://private-test-evidence/investigations/synthetic-financial-demo/run-123.json"
