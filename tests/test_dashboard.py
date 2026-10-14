from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ui.dashboard import SCHEMA, load_dashboard, ordered_artifacts


def dashboard_fixture(workspace: Path) -> tuple[dict, list]:
    (workspace / "results").mkdir(exist_ok=True)
    document = {
        "schema": SCHEMA,
        "title": "Verified results",
        "summary": "Two source rows were included in this saved analysis.",
        "metrics": [{"label": "Rows analyzed", "value": "2", "help": "Source row count"}],
        "findings": ["The total is 30."],
        "charts": ["results/chart.json"],
        "provenance": "Computed from the test dataset.",
    }
    (workspace / "results/dashboard.json").write_text(json.dumps(document), encoding="utf-8")
    (workspace / "results/chart.json").write_text(json.dumps({
        "data": [{"type": "bar", "x": ["A", "B"], "y": [10, 20]}],
        "layout": {"title": {"text": "Values"}},
    }), encoding="utf-8")
    artifacts = [
        {"location": "results/dashboard.json", "name": "Dashboard", "type": "json", "validation_status": "PASSED"},
        {"location": "results/chart.json", "name": "Values", "type": "chart", "validation_status": "PASSED"},
    ]
    return document, artifacts


def test_dashboard_loads_saved_metrics_and_registered_chart(tmp_path):
    _, artifacts = dashboard_fixture(tmp_path)
    report = load_dashboard(tmp_path, artifacts, {"passed": True})
    assert report["metrics"] == [{"label": "Rows analyzed", "value": "2", "help": "Source row count"}]
    assert report["findings"] == ["The total is 30."]
    assert list(report["charts"][0].data[0].y) == [10, 20]


@pytest.mark.parametrize("validation", [{}, {"passed": False}, {"passed": "true"}])
def test_failed_or_absent_overall_validation_does_not_read_dashboard(tmp_path, validation):
    _, artifacts = dashboard_fixture(tmp_path)
    (tmp_path / "results/dashboard.json").unlink()
    assert load_dashboard(tmp_path, artifacts, validation) is None


@pytest.mark.parametrize("status", ["FAILED", "DISCOVERED", None])
def test_dashboard_requires_passed_registration(tmp_path, status):
    _, artifacts = dashboard_fixture(tmp_path)
    artifacts[0]["validation_status"] = status
    assert load_dashboard(tmp_path, artifacts, {"passed": True}) is None


def test_unregistered_dashboard_and_unrecognized_json_are_not_rendered(tmp_path):
    _, artifacts = dashboard_fixture(tmp_path)
    assert load_dashboard(tmp_path, artifacts[1:], {"passed": True}) is None
    (tmp_path / "results/dashboard.json").write_text('{"ordinary": "result"}', encoding="utf-8")
    assert load_dashboard(tmp_path, artifacts, {"passed": True}) is None


@pytest.mark.parametrize("change", ["unregistered", "failed", "missing", "traversal"])
def test_chart_must_be_registered_passed_existing_and_confined(tmp_path, change):
    document, artifacts = dashboard_fixture(tmp_path)
    if change == "unregistered":
        artifacts.pop()
    elif change == "failed":
        artifacts[1]["validation_status"] = "FAILED"
    elif change == "missing":
        (tmp_path / "results/chart.json").unlink()
    else:
        document["charts"] = ["../outside.json"]
        artifacts[1]["location"] = "../outside.json"
        (tmp_path / "results/dashboard.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        load_dashboard(tmp_path, artifacts, {"passed": True})


@pytest.mark.parametrize("chart", [
    {"data": [], "layout": {"images": [{"source": "https://external.invalid/image.png"}]}},
    {"data": [], "layout": {"annotations": [{"text": '<img src="//external.invalid/image.png">'}]}},
    {"data": [{"type": "scattermapbox", "lat": [1], "lon": [2]}]},
    {"data": [], "layout": {"images": [{"source": "data:image/svg+xml,<svg/>"}]}},
])
def test_chart_external_resources_are_refused(tmp_path, chart):
    _, artifacts = dashboard_fixture(tmp_path)
    (tmp_path / "results/chart.json").write_text(json.dumps(chart), encoding="utf-8")
    with pytest.raises(ValueError):
        load_dashboard(tmp_path, artifacts, {"passed": True})


@pytest.mark.parametrize("change", ["too_many_metrics", "long_finding", "large_json", "invalid_metric"])
def test_report_content_is_bounded_and_typed(tmp_path, change):
    document, artifacts = dashboard_fixture(tmp_path)
    if change == "too_many_metrics":
        document["metrics"] *= 9
    elif change == "long_finding":
        document["findings"] = ["x" * 1001]
    elif change == "large_json":
        document["summary"] = "x" * (2 * 1024 * 1024)
    else:
        document["metrics"][0]["value"] = {"unexpected": "object"}
    (tmp_path / "results/dashboard.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        load_dashboard(tmp_path, artifacts, {"passed": True})


def test_results_prioritize_declared_outputs_without_changing_registry():
    artifacts = [
        {"location": "audit.ingestion", "type": "database_table", "validation_status": "DISCOVERED"},
        {"location": "generated/solution.py", "type": "python_script", "validation_status": "DISCOVERED"},
        {"location": "results/other.json", "type": "json", "validation_status": "DISCOVERED"},
        {"location": "notebooks/solution.ipynb", "type": "notebook", "validation_status": "PASSED"},
        {"location": "results/dashboard.json", "type": "json", "validation_status": "PASSED"},
        {"location": "results.summary", "type": "database_table", "validation_status": "PASSED"},
    ]
    original = copy.deepcopy(artifacts)
    ordered = ordered_artifacts(artifacts, [{"location": "results.summary"}])
    assert [item["location"] for item in ordered] == [
        "results.summary", "results/dashboard.json", "generated/solution.py",
        "notebooks/solution.ipynb", "results/other.json", "audit.ingestion",
    ]
    assert artifacts == original


def test_results_app_renders_dashboard_and_keeps_query_tools(tmp_path, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    from workbench.data import ingest_csv
    from workbench.service import Service
    from workbench.settings import Settings
    from workbench.workspace import WorkspaceManager

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setattr(Service, "diagnostics", lambda self: {})
    st.cache_resource.clear()
    try:
        settings = Settings.from_env()
        manager = WorkspaceManager(settings.workspace_root)
        workspace = manager.create("Dashboard test", "Summarize values")
        ingest_csv(workspace, b"id,value\nA,10\nB,20\n", "test.csv", settings)
        _, artifacts = dashboard_fixture(workspace)
        manager.write(workspace, "metadata/manifest.json", {"artifacts": artifacts})
        manager.write(workspace, "metadata/validation.json", {"passed": True, "checks": []})
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20)
        app.session_state["workspace_id"] = workspace.name
        app.session_state["page"] = "Results"
        app.run()
        assert not app.exception
        assert any(item.label == "Rows analyzed" and item.value == "2" for item in app.metric)
        assert any("The total is 30." in item.value for item in app.text)
        assert any(item.label == "Run bounded query" for item in app.button)
        assert any(item.label == "Dashboard" for item in app.expander)

        # A broken dashboard is a recoverable warning; saved artifacts/query tools remain.
        (workspace / "results/chart.json").unlink()
        app.run()
        assert not app.exception
        assert not app.metric
        assert any("Dashboard preview is unavailable" in item.value for item in app.warning)
        assert any(item.label == "Run bounded query" for item in app.button)
    finally:
        st.cache_resource.clear()
