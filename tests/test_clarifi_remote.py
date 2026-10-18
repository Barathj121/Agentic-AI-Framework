import copy
import json

import httpx
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("langgraph")

from workbench.agents.clarifi import remote
from workbench.agents.clarifi.agent import ClariFiAgent
from workbench.agents.clarifi.ingest import sample_input
from workbench.service import Service
from workbench.settings import Settings


@pytest.fixture
def cloud(monkeypatch):
    # Both Cloud Run hostname formats are accepted, including region suffixes.
    monkeypatch.setenv("CLARIFI_API_URL", "https://clarifi-test-123.us-central1.run.app")
    monkeypatch.setenv("CLARIFI_REMOTE_MODEL", "gemini-test")
    source = sample_input("combined")
    report = ClariFiAgent(mode="rules").run(source, approved=True)
    report["provenance"].update(model="gemini-test", ai_used=True)
    report["evidence_uri"] = "gs://private-evidence/investigations/test.json"
    calls = []
    original = httpx.Client

    def install(handler):
        def capture(request):
            calls.append(request)
            return handler(request)
        monkeypatch.setattr(remote.httpx, "Client", lambda **kwargs: original(
            transport=httpx.MockTransport(capture), **kwargs))
    monkeypatch.setattr(remote, "identity_token", lambda audience: "private-identity-token")
    return source, report, calls, install


def test_approved_service_handoff_binds_input_and_model(cloud, tmp_path):
    source, report, calls, install = cloud
    install(lambda request: httpx.Response(200, json=report))
    actual = Service(Settings(workspace_root=tmp_path)).run_agent(
        "clarifi", source.model_dump(mode="json"), approved=True, mode="remote", model="gemini-test")
    assert actual == report
    assert len(calls) == 1
    assert calls[0].url.path == "/investigations"
    assert calls[0].headers["Authorization"] == "Bearer private-identity-token"
    body = json.loads(calls[0].content)
    assert body == {"approved": True, "model": "gemini-test", "data": source.model_dump(mode="json")}


@pytest.mark.parametrize("approved", [False, "true", 1, None])
def test_unapproved_requests_never_leave_host(cloud, approved):
    source, report, calls, install = cloud
    install(lambda request: httpx.Response(200, json=report))
    with pytest.raises(ValueError, match="approval"):
        remote.investigate(source, approved=approved, model="gemini-test")
    assert not calls


@pytest.mark.parametrize("status", [302, 401, 403, 429, 502])
def test_errors_are_redacted_without_retry_or_fallback(cloud, status):
    source, _, calls, install = cloud
    install(lambda request: httpx.Response(status, text="private-identity-token sensitive financial data"))
    with pytest.raises(remote.RemoteClariFiError) as caught:
        remote.investigate(source, approved=True, model="gemini-test")
    assert str(status) in str(caught.value)
    assert "sensitive" not in str(caught.value)
    assert "private-identity-token" not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [("input_sha256", "other-input"), ("case_id", "other-case"),
                                         ("status", "FAILED"), ("evidence_uri", None)])
def test_mismatched_or_unpersisted_report_is_rejected(cloud, field, value):
    source, report, _, install = cloud
    report = copy.deepcopy(report)
    report[field] = value
    install(lambda request: httpx.Response(200, json=report))
    with pytest.raises(remote.RemoteClariFiError, match="contract"):
        remote.investigate(source, approved=True, model="gemini-test")


@pytest.mark.parametrize("url", ["http://localhost:8080", "https://example.com", "https://x.run.app/path",
                                 "https://x.run.app?token=secret", "https://user:secret@x.run.app"])
def test_only_configured_cloud_run_origin_receives_identity(monkeypatch, url):
    monkeypatch.setenv("CLARIFI_API_URL", url)
    with pytest.raises(remote.RemoteClariFiError):
        remote.service_url()


def test_model_change_requires_fresh_configuration_and_approval(cloud):
    source, _, calls, _ = cloud
    with pytest.raises(remote.RemoteClariFiError, match="changed"):
        remote.investigate(source, approved=True, model="different-model")
    assert not calls
