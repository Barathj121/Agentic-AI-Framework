"""Independent smoke assertions use real DuckDB results, without a model or Docker."""
from __future__ import annotations

import duckdb
import pytest
from filelock import FileLock, Timeout

from scripts.smoke_test import verify_smoke_result
from workbench.service import Service
from workbench.settings import Settings


@pytest.fixture
def smoke_workspace(tmp_path):
    service = Service(Settings(workspace_root=tmp_path / "workspaces"))
    content = b"customer_id,amount\nC1,10\nC1,15\nC2,20\nC2,25\nC3,30\nC3,35\n"
    workspace = service.create_workspace("Smoke verification", "Compute customer totals", content, "smoke.csv")
    manager = service.manager
    original_check = {"name": "Original generic structural validation", "kind": "table_exists",
                      "target": "results.customer_totals", "passed": True}
    manager.write(workspace, "metadata/validation.json", {"passed": True, "checks": [original_check],
                  "errors": [], "artifacts": [{"name": "Totals", "location": "results.customer_totals"}]})
    manager.write(workspace, "metadata/manifest.json", {"workspace_id": workspace.name, "status": "COMPLETED",
                  "validation_passed": True, "artifacts": [{"name": "Totals", "location": "results.customer_totals", "validation_status": "PASSED"}]})
    manager.write(workspace, "metadata/execution.json", {"success": True, "exit_code": 0, "test_fixture": True})
    manager.write(workspace, "metadata/attempts/0/validation.json", {"passed": True, "checks": [original_check]})
    manager.write(workspace, "metadata/state.json", {"status": "COMPLETED", "test_fixture": True})
    (workspace / "metadata/checkpoints.sqlite").write_bytes(b"Explicit fixture of durable generic stage history")
    manager.status(workspace, "COMPLETED")
    return service, workspace, original_check


def create_totals(workspace, rows):
    with duckdb.connect(str(workspace / "database/workspace.duckdb")) as connection:
        connection.execute("CREATE OR REPLACE TABLE results.customer_totals (customer_id VARCHAR, total_amount INTEGER)")
        connection.executemany("INSERT INTO results.customer_totals VALUES (?,?)", rows)


def historical_files(workspace):
    return {name: (workspace / name).read_bytes() for name in (
        "metadata/execution.json", "metadata/checkpoints.sqlite", "metadata/state.json", "metadata/attempts/0/validation.json",
    )}


def test_exact_rows_pass_and_preserve_generic_evidence(smoke_workspace):
    service, workspace, original_check = smoke_workspace
    create_totals(workspace, [("C3", 65), ("C1", 25), ("C2", 45)])
    before = historical_files(workspace)
    result = verify_smoke_result(service, workspace)
    assert result["passed"] is True
    assert result["actual"] == result["expected"] == [["C1", 25], ["C2", 45], ["C3", 65]]
    assert service.manager.read(workspace, "metadata/smoke_validation.json") == result
    validation = service.manager.read(workspace, "metadata/validation.json")
    assert validation["passed"] and validation["smoke_test_passed"]
    assert validation["checks"][0] == original_check
    assert len(validation["checks"]) == 2
    assert validation["generic_validation_passed"] is True
    assert service.manager.read(workspace, "metadata/manifest.json")["smoke_test_passed"] is True
    assert service.status(workspace.name)["status"] == "COMPLETED"
    assert before == historical_files(workspace)


@pytest.mark.parametrize("rows", [
    [("C1", 26), ("C2", 44), ("C3", 65)],  # Correct grand total, incorrect customers.
    [("C1", 25), ("C2", 45), ("C3", 65), ("C4", 0)],
    [("C1", 25), ("C2", 45)],
])
def test_wrong_rows_fail_even_when_total_is_135(smoke_workspace, rows):
    service, workspace, original_check = smoke_workspace
    create_totals(workspace, rows)
    before = historical_files(workspace)
    result = verify_smoke_result(service, workspace)
    assert result["passed"] is False
    assert result["error"].startswith("Exact customer totals differ")
    assert service.status(workspace.name)["status"] == "FAILED"
    assert service.status(workspace.name)["failure_stage"] == "INDEPENDENT_SMOKE_VALIDATION"
    manifest = service.manager.read(workspace, "metadata/manifest.json")
    assert manifest["status"] == "FAILED" and manifest["smoke_test_passed"] is False
    assert manifest["validation_passed"] is False
    assert manifest["artifacts"][0]["validation_status"] == "FAILED"
    validation = service.manager.read(workspace, "metadata/validation.json")
    assert not validation["passed"] and validation["checks"][0] == original_check
    assert validation["checks"][-1]["evidence"] == result
    assert validation["errors"]
    assert before == historical_files(workspace)


@pytest.mark.parametrize("missing", ["table", "column"])
def test_missing_table_or_column_always_persists_failed_evidence(smoke_workspace, missing):
    service, workspace, original_check = smoke_workspace
    if missing == "column":
        with duckdb.connect(str(workspace / "database/workspace.duckdb")) as connection:
            connection.execute("CREATE TABLE results.customer_totals AS SELECT 'C1' AS customer_id, 25 AS wrong_column")
    before = historical_files(workspace)
    result = verify_smoke_result(service, workspace)
    assert result["passed"] is False
    assert result["actual"] is None
    assert result["expected"] == [["C1", 25], ["C2", 45], ["C3", 65]]
    assert "Could not query" in result["error"]
    assert service.manager.read(workspace, "metadata/smoke_validation.json") == result
    validation = service.manager.read(workspace, "metadata/validation.json")
    assert validation["checks"][0] == original_check
    assert validation["independent_smoke_validation"] == result
    assert service.status(workspace.name)["status"] == "FAILED"
    assert before == historical_files(workspace)


def test_passing_recheck_does_not_erase_failure_history(smoke_workspace):
    service, workspace, original_check = smoke_workspace
    create_totals(workspace, [("C1", 26), ("C2", 44), ("C3", 65)])
    first = verify_smoke_result(service, workspace)
    create_totals(workspace, [("C1", 25), ("C2", 45), ("C3", 65)])
    second = verify_smoke_result(service, workspace)
    assert not first["passed"] and second["passed"]
    validation = service.manager.read(workspace, "metadata/validation.json")
    assert validation["checks"][0] == original_check
    assert [item["passed"] for item in validation["checks"]] == [True, False, True]
    assert validation["passed"] is False
    assert service.status(workspace.name)["status"] == "FAILED"
    assert service.status(workspace.name)["smoke_test_passed"] is True


def test_active_workflow_lock_prevents_benchmark_metadata_changes(smoke_workspace):
    service, workspace, _ = smoke_workspace
    before = (workspace / "metadata/validation.json").read_bytes()
    with FileLock(str(workspace / "metadata/workflow.lock"), timeout=0):
        with pytest.raises(Timeout):
            verify_smoke_result(service, workspace)
    assert (workspace / "metadata/validation.json").read_bytes() == before
    assert not (workspace / "metadata/smoke_validation.json").exists()
