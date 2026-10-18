from __future__ import annotations

import json
from pathlib import Path

import pytest

from ui.helpers import chart_has_external_resources, notebook_preview, proposals_from, quoted_table, read_text


def test_previews_are_bounded_and_paths_are_confined(tmp_path):
    (tmp_path / "report.txt").write_text("abcdefghijk", encoding="utf-8")
    assert read_text(tmp_path, "report.txt", limit=5).startswith("abcde")
    assert "truncated" in read_text(tmp_path, "report.txt", limit=5)
    with pytest.raises(ValueError):
        read_text(tmp_path, "../outside.txt")


def test_notebook_preview_excludes_executable_html_and_javascript():
    document = {"cells": [{"cell_type": "code", "source": ["print('result')"], "outputs": [
        {"data": {"text/html": "<script>malicious()</script>", "application/javascript": "malicious()", "text/plain": "result"}}
    ]}]}
    result = notebook_preview(document)
    assert result[0]["outputs"] == ["result"]
    assert "malicious" not in json.dumps(result)


def test_table_identifiers_and_external_chart_resources():
    assert quoted_table("results.customer_scores") == '"results"."customer_scores"'
    for value in ["results.x; DROP TABLE x", "read_csv('/etc/passwd')", "raw.x.y", "results.x--"]:
        with pytest.raises(ValueError):
            quoted_table(value)
    assert chart_has_external_resources({"layout": {"images": [{"source": "https://external.invalid/a.png"}]}})
    assert not chart_has_external_resources({"data": [{"type": "bar", "x": [1], "y": [2]}]})
    assert proposals_from({"proposals": [{"proposal_id": "a"}]}) == [{"proposal_id": "a"}]


@pytest.fixture
def app_environment(tmp_path, monkeypatch):
    import streamlit as st
    from workbench.service import Service
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setattr(Service, "diagnostics", lambda self: {
        "ollama": {"available": False, "model_available": False},
        "docker": {"available": False, "image_available": False},
    })
    st.cache_resource.clear()
    yield tmp_path
    st.cache_resource.clear()


def test_initial_ui_and_empty_history_do_not_require_services(app_environment):
    from streamlit.testing.v1 import AppTest
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=20).run()
    assert not app.exception
    assert app.header[0].value == "Agentic Data & Application Workbench"
    next(item for item in app.button if item.label == "Open Data Workbench").click().run()
    assert app.header[0].value == "Start with your data"
    assert any(item.label == "Create & profile workspace" for item in app.button)
    app.sidebar.radio[0].set_value("Workspace history").run()
    assert not app.exception
    assert any("saved workspaces" in item.value for item in app.info)


def test_history_reopens_a_profile_and_results_query(app_environment):
    from streamlit.testing.v1 import AppTest
    from workbench.data import ingest_csv
    from workbench.settings import Settings
    from workbench.workspace import WorkspaceManager
    settings = Settings.from_env()
    manager = WorkspaceManager(settings.workspace_root)
    workspace = manager.create("Persisted dataset", "Summarize values")
    ingest_csv(workspace, b"id,value\nA,10\nB,20\n", "test.csv", settings)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
    app.sidebar.radio[0].set_value("Workspace history").run()
    next(button for button in app.button if button.label == "Open workspace").click().run()
    assert not app.exception
    assert any(item.value == "Persisted dataset" for item in app.subheader)
    app.sidebar.radio[0].set_value("Results").run()
    assert not app.exception
    next(button for button in app.button if button.label == "Run bounded query").click().run()
    assert not app.exception
    assert len(app.dataframe[0].value) == 2


@pytest.mark.parametrize("status,legacy", [
    ("MIGRATION_COMPLETED", False),
    ("MIGRATION_INCOMPLETE", False),
    ("MIGRATION_FAILED", True),
    ("MAPPING_REVIEW", False),
    ("MAPPING_REVIEW_PAUSED", False),
])
def test_migration_history_filters_and_reopens_saved_runs(app_environment, monkeypatch, status, legacy):
    import duckdb
    import pandas as pd
    from streamlit.testing.v1 import AppTest
    from workbench.migration import Mapping, MappingPlan, MigrationService
    from workbench.migration_runtime import materialize_and_validate
    from workbench.settings import Settings

    def forbidden_model_or_execution(*args, **kwargs):
        raise AssertionError("Browsing saved history must not regenerate or execute a migration")

    monkeypatch.setattr(MigrationService, "propose", forbidden_model_or_execution)
    monkeypatch.setattr(MigrationService, "execute", forbidden_model_or_execution)
    service = MigrationService(Settings.from_env())
    service.manager.create("Unrelated data workspace", "Keep generic workspaces in All")
    workspace = service.create("Saved payroll migration", b"identifier\nX1\nX2\n",
        b"column_name,data_type,required,description\nnew_identifier,VARCHAR,true,Employee identifier\n")
    missing = status == "MIGRATION_INCOMPLETE"
    plan = MappingPlan(mappings=[Mapping(target_column="new_identifier", source_columns=[] if missing else ["identifier"],
        mapping_type="MISSING" if missing else "DIRECT", status="MISSING" if missing else "APPROVED", reason="Reviewed identifier relationship")])
    service.manager.write(workspace, "generated/mapping.json", plan.model_dump())
    if status != "MAPPING_REVIEW":
        service.approve(workspace.name, plan)
    if status in {"MIGRATION_COMPLETED", "MIGRATION_INCOMPLETE", "MAPPING_REVIEW_PAUSED"}:
        _, target = service.metadata(workspace.name)
        values = pd.DataFrame({"new_identifier": [None, None] if missing else ["X1", "X2"]})
        with duckdb.connect(str(workspace / "database/workspace.duckdb")) as con:
            report = materialize_and_validate(con, values, [field.model_dump() for field in target],
                [mapping.model_dump() for mapping in plan.mappings], 2)
        service.manager.write(workspace, "results/validation.json", report)
        report = service._verify_report(workspace, target, plan)
        service.manager.write(workspace, "metadata/migration_result.json", report)
        (workspace / "results/migrated_data.csv").write_text(values.to_csv(index=False), encoding="utf-8")
    service.manager.status(workspace, "MIGRATION_COMPLETED" if status == "MAPPING_REVIEW_PAUSED" else status,
        error="ValueError: generated transformation failed" if status == "MIGRATION_FAILED" else None)
    if status == "MAPPING_REVIEW_PAUSED":
        service.manager.write(workspace, "metadata/mapping_review.json", {"invalidation_pending": True})
    if legacy:
        metadata = service.manager.read(workspace, "metadata/workspace.json")
        metadata.pop("kind")
        service.manager.write(workspace, "metadata/workspace.json", metadata)

    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
    app.sidebar.radio[0].set_value("Migration Studio").run()
    next(item for item in app.button if item.label == "Migration history").click().run()
    assert not app.exception
    assert app.session_state["page"] == "Workspace history"
    kind_filter = next(item for item in app.selectbox if item.label == "Workspace type")
    assert kind_filter.value == "Migrations"
    assert any(item.value == "Saved payroll migration" for item in app.subheader)
    assert not any(item.value == "Unrelated data workspace" for item in app.subheader)
    expected_status = "Mapping review paused" if status == "MAPPING_REVIEW_PAUSED" else status
    assert any(expected_status in item.value and "Updated" in item.value for item in app.caption)
    assert any("Source records: 2" in item.value for item in app.caption)
    if status in {"MIGRATION_COMPLETED", "MIGRATION_INCOMPLETE"}:
        assert any("Target records: 2" in item.value and "Invalid: " in item.value for item in app.caption)
    elif status == "MAPPING_REVIEW_PAUSED":
        assert not any("Target records:" in item.value or "MIGRATION_COMPLETED" in item.value for item in app.caption)
    kind_filter.set_value("All").run()
    assert any(item.value == "Unrelated data workspace" for item in app.subheader)
    next(item for item in app.selectbox if item.label == "Workspace type").set_value("Migrations").run()
    next(item for item in app.button if item.key == f"open_{workspace.name}").click().run()
    assert not app.exception
    assert app.session_state["page"] == "Migration Studio"
    assert app.session_state["migration_workspace_id"] == workspace.name
    assert next(item for item in app.selectbox if item.label == "Migration workspace").value == workspace.name
    assert not any(item.value == "Results & evidence" for item in app.header)
    if status == "MIGRATION_FAILED":
        assert any(item.label == "Retry approved migration" for item in app.button)
    elif status == "MAPPING_REVIEW_PAUSED":
        assert any(item.label == "Resume mapping review" for item in app.button)
        assert not any(item.proto.label == "Download target CSV" for item in app.get("download_button"))
    elif status == "MAPPING_REVIEW":
        assert any(item.label == "Approve reviewed mapping plan" for item in app.button)
    else:
        assert any(item.proto.label == "Download target CSV" for item in app.get("download_button"))


def test_synthetic_data_has_seeded_patterns_and_reproducible_sales():
    from examples.generate_data import generate_banking, generate_sales
    rows, annotations = generate_banking(1000, 42)
    assert len(rows) == len(annotations) == 1000
    assert {item["scenario"] for item in annotations} >= {
        "large_amount", "repeated_small_transfers", "high_frequency", "behavior_shift", "common_destination"
    }
    assert all("scenario" not in row and "is_injected_pattern" not in row for row in rows)
    assert rows == generate_banking(1000, 42)[0]
    sales = generate_sales(100, 43)
    assert sales == generate_sales(100, 43)
    assert all(row["revenue"] == round(row["quantity"] * row["unit_price"] * (1 - row["discount_rate"]), 2) for row in sales)


def test_ui_approval_requires_checkpoint_and_explicit_click(app_environment, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from scripts.fixture_demo import scenario_contract
    from workbench.service import Service
    from workbench.settings import Settings
    service = Service(Settings.from_env())
    workspace = service.create_workspace("Approval gate", "Count values", b"customer_id,transaction_amount,transaction_type\nA,10,transfer\n", "input.csv")
    service.manager.write(workspace, "metadata/proposals.json", scenario_contract("sql")["proposal_set"])
    approvals = []

    def approve(instance, workspace_id, proposal_id, instructions=""):
        approvals.append((workspace_id, proposal_id, instructions))
        instance.manager.status(instance.manager.get(workspace_id), "EXECUTING")

    monkeypatch.setattr(Service, "approve", approve)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20)
    app.session_state["workspace_id"] = workspace.name
    app.session_state["page"] = "Solution proposals"
    app.run()
    assert not app.exception
    assert not any(button.label == "Approve & implement" for button in app.button)
    assert not approvals
    service.manager.status(workspace, "AWAITING_APPROVAL")
    app.run()
    assert not app.exception
    assert not approvals
    next(button for button in app.button if button.label == "Approve & implement").click().run()
    assert not app.exception
    assert approvals == [(workspace.name, "fixture_sql", "")]
    assert app.header[0].value == "Execution monitor"
