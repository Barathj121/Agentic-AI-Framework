import json
from pathlib import Path

import pytest

pytest.importorskip("sklearn")

from workbench.agents.clarifi.agent import ClariFiAgent
from workbench.agents.clarifi.ingest import sample_input, validate_scope


@pytest.fixture
def financial_ui(tmp_path, monkeypatch):
    import streamlit as st
    from workbench.service import Service
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setattr(Service, "diagnostics", lambda self: {})
    st.cache_resource.clear()
    yield tmp_path
    st.cache_resource.clear()


def open_ui():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30)
    app.session_state["page"] = "ClariFi Agent"
    return app.run()


def test_financial_intake_is_separate_and_requires_approval(financial_ui):
    app = open_ui()
    assert not app.exception
    assert app.header[0].value == "ClariFi Agent"
    run = next(button for button in app.button if button.label == "Run Investigation")
    assert run.disabled
    assert not app.metric
    assert any(item.label == "Check local model" for item in app.button)
    assert any(item.value == "ClariFi Capabilities" for item in app.subheader)
    assert any("Onboarding a new company?" in item.value for item in app.markdown)
    assert any(item.label == "Download Excel template" for item in app.get("download_button"))


def test_gemini_api_selection_uses_hidden_server_configuration(financial_ui, monkeypatch):
    monkeypatch.setattr("workbench.agents.clarifi.models.gemini_api_config",
                        lambda: {"key": "fixture-private-key", "model": "gemini-3.8-flash"})
    app = open_ui()
    next(item for item in app.selectbox if item.label == "Reasoning model").set_value("Gemini API · AI Studio").run()
    assert not app.exception
    assert next(item for item in app.text_input if item.label == "Gemini API model ID").value == "gemini-3.8-flash"
    assert any(item.label == "Check Gemini API" for item in app.button)
    assert not any("fixture-private-key" in item.value for item in list(app.caption) + list(app.markdown) + list(app.text))
    assert next(button for button in app.button if button.label == "Run Investigation").disabled


@pytest.mark.parametrize("capability", ["Insider trading indicators", "Shell-company networks"])
def test_both_capabilities_run_render_and_save_reports(financial_ui, capability):
    app = open_ui()
    next(item for item in app.radio if item.label == "Choose your capability").set_value(capability).run()
    next(item for item in app.selectbox if item.label == "Reasoning model").set_value("Offline rules demo").run()
    app.checkbox[0].check().run()
    next(button for button in app.button if button.label == "Run Investigation").click().run()
    assert not app.exception
    assert any(item.label == "Findings" for item in app.metric)
    assert any(item.label == "Download investigation report" for item in app.get("download_button"))
    files = list((financial_ui / "workspaces/clarifi").glob("*.json"))
    assert len(files) == 1
    bundle = json.loads(files[0].read_text())
    assert not bundle["report"]["provenance"]["ai_used"]
    expected = "insider_trading" if capability.startswith("Insider") else "shell_companies"
    assert {f["capability"] for f in bundle["report"]["findings"]} == {expected}
    # Reopening is possible after a fresh app session; no original CSV workspace required.
    reopened = open_ui()
    next(button for button in reopened.button if button.label == "Open financial report").click().run()
    assert not reopened.exception
    assert reopened.metric


def test_changed_capability_hides_stale_results(financial_ui):
    app = open_ui()
    next(item for item in app.selectbox if item.label == "Reasoning model").set_value("Offline rules demo").run()
    app.checkbox[0].check().run()
    next(button for button in app.button if button.label == "Run Investigation").click().run()
    next(item for item in app.radio if item.label == "Choose your capability").set_value("Shell-company networks").run()
    assert not app.exception
    assert not app.metric
    assert next(button for button in app.button if button.label == "Run Investigation").disabled


def test_failed_investigation_does_not_display_completed_metrics(financial_ui, monkeypatch):
    from workbench.service import Service
    def fail(*args, **kwargs):
        raise ValueError("Invalid model plan")
    monkeypatch.setattr(Service, "run_agent", fail)
    app = open_ui()
    next(item for item in app.selectbox if item.label == "Reasoning model").set_value("Offline rules demo").run()
    app.checkbox[0].check().run()
    next(button for button in app.button if button.label == "Run Investigation").click().run()
    assert not app.exception
    assert app.error
    assert not app.metric
    assert not list((financial_ui / "workspaces/clarifi").glob("*.json"))


def test_financial_screen_does_not_accept_combined_or_wrong_scope():
    with pytest.raises(ValueError, match="selected"):
        validate_scope(sample_input("shell_companies"), "insider_trading")


def test_local_reasoner_reuses_structured_client_without_changing_csv_settings(monkeypatch):
    from workbench.agents.clarifi.models import LocalReasoner
    from workbench.agents.clarifi.schemas import Plan
    from workbench.llm import OllamaClient
    from workbench.settings import Settings
    observed = {}
    def structured(client, schema, instruction, prompt):
        observed["model"] = client.model
        observed["prompt"] = json.loads(prompt)
        return Plan(tools=["insider_trading"], rationale="Model-backed plan contract")
    monkeypatch.setattr(OllamaClient, "structured", structured)
    original = Settings().ollama_model
    reasoner = LocalReasoner("gemma3:4b")
    assert reasoner.plan({"available_tools": ["insider_trading"]}).tools == ["insider_trading"]
    assert observed["model"] == "gemma3:4b"
    assert Settings().ollama_model == original


def test_model_plan_schema_excludes_unavailable_tools():
    from workbench.agents.clarifi.models import scoped_plan_schema
    from pydantic import ValidationError
    schema = scoped_plan_schema({"available_tools": ["shell_companies"]})
    with pytest.raises(ValidationError):
        schema.model_validate({"tools": ["insider_trading"], "rationale": "Unavailable tool"})
    with pytest.raises(ValidationError):
        schema.model_validate({"tools": ["shell_companies", "shell_companies"], "rationale": "Duplicate tool"})
    assert schema.model_validate({"tools": ["shell_companies"], "rationale": "Review payment evidence"}).tools == ["shell_companies"]


def test_cyclic_payment_chart_retains_directions_and_evidence():
    from ui.clarifi import payment_network
    data = sample_input("shell_companies").model_dump(mode="json")
    chart = payment_network(data["payments"])
    assert len(chart.layout.annotations) == 4
    assert any("P03" in trace.text[0] for trace in chart.data if trace.mode == "lines")


@pytest.mark.parametrize("tool", ["insider_trading", "shell_companies"])
def test_excel_template_round_trip_preserves_investigation(tool):
    from workbench.agents.clarifi.ingest import excel_template, read_excel_bytes
    from workbench.agents.clarifi.schemas import InvestigationInput
    sample = sample_input(tool)
    restored = InvestigationInput.model_validate({"case_id": sample.case_id, **read_excel_bytes(excel_template(tool), tool)})
    assert restored == sample
    report = ClariFiAgent(mode="rules").run(restored, approved=True)
    assert report["findings"]


def test_excel_rejects_formulas_and_wrong_capability():
    import io
    from openpyxl import load_workbook
    from workbench.agents.clarifi.ingest import excel_template, read_excel_bytes
    content = excel_template("shell_companies")
    with pytest.raises(ValueError, match="outside"):
        read_excel_bytes(content, "insider_trading")
    workbook = load_workbook(io.BytesIO(content))
    workbook["entities"]["B2"] = "=1+1"
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    with pytest.raises(ValueError, match="formulas"):
        read_excel_bytes(output.getvalue(), "shell_companies")
