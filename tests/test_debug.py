"""Manual debugging snapshots remain isolated and preserve the approved output contract."""
from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pytest
from filelock import FileLock

from workbench.debug import debug_solution
from workbench.schemas import BuildResult, ExecutionResult, Proposal
from workbench.service import Service
from workbench.settings import Settings


@pytest.fixture
def review_workspace(tmp_path):
    settings = Settings(workspace_root=tmp_path / "workspaces", sandbox_timeout_seconds=30)
    service = Service(settings)
    workspace = service.create_workspace("Reviewed aggregation", "Sum the amount", b"id,amount\n1,10\n2,20\n", "data.csv")
    plan = Proposal(proposal_id="sum", title="Sum amounts", description="Calculate total", approach="SQL aggregation",
                    implementation_steps=["Sum amount", "Save JSON"], required_columns=["amount"],
                    expected_outputs=[{"name": "Total", "type": "json", "location": "results/total.json"}],
                    advantages=["Deterministic"], limitations=["Descriptive"], assumptions=[],
                    validation_strategy=["Read JSON"], feasibility="FEASIBLE", confidence_explanation="Numeric column exists")
    build = BuildResult(files=[{"path": "generated/solution.py", "content": "raise RuntimeError('old source must not execute')\n"}],
                        expected_outputs=plan.expected_outputs,
                        validation_checks=[{"name": "Total JSON", "kind": "json_valid", "target": "results/total.json"}])
    service.manager.write(workspace, "metadata/selected_plan.json", {"proposal": plan.model_dump(mode="json"), "instructions": "Approved", "approved_at": "2026-10-15"})
    service.manager.write(workspace, "metadata/build.json", build.model_dump(mode="json"))
    (workspace / "generated/solution.py").write_text(build.files[0].content, encoding="utf-8")
    (workspace / "metadata/checkpoints.sqlite").write_bytes(b"original checkpoint history")
    (workspace / "results/total.json").write_text('{"total":999}', encoding="utf-8")
    service.manager.status(workspace, "COMPLETED")
    return settings, service, workspace


class FixtureSandbox:
    """Never evaluates generated code; records arguments and writes explicit fixture output."""
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def execute(self, workspace, build, attempt):
        self.calls.append((workspace, build, attempt))
        assert not (workspace / "results/total.json").exists()
        assert not (workspace / "metadata/checkpoints.sqlite").exists()
        with duckdb.connect(str(workspace / "database/workspace.duckdb"), read_only=True) as connection:
            assert connection.execute("SELECT sum(amount) FROM raw.source").fetchone()[0] == 30
        (workspace / "logs/attempt-0.stdout.log").write_text("Explicit test fixture", encoding="utf-8")
        (workspace / "logs/attempt-0.stderr.log").write_text("fixture failure" if self.fail else "", encoding="utf-8")
        if not self.fail:
            (workspace / "results/total.json").write_text('{"total":30,"test_fixture":true}', encoding="utf-8")
        return ExecutionResult(success=not self.fail, exit_code=1 if self.fail else 0, execution_time_seconds=0,
                               stdout_path="logs/attempt-0.stdout.log", stderr_path="logs/attempt-0.stderr.log",
                               error="fixture failure" if self.fail else None)

    def inspect(self, workspace, checks):
        return {"checks": [], "artifacts": []}


def test_review_requires_explicit_execution_and_never_runs_source_or_model(review_workspace, monkeypatch):
    settings, service, workspace = review_workspace
    marker = workspace.parent / "must_not_exist.txt"
    (workspace / "generated/solution.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe host execution')\n", encoding="utf-8")
    def forbidden(*args, **kwargs):
        raise AssertionError("Model construction is forbidden for manual review")
    import workbench.workflow as module
    monkeypatch.setattr(module, "create_llm", forbidden)
    runtime = FixtureSandbox()
    report = debug_solution(settings, workspace.name, sandbox=runtime)
    assert report["dry_run"] and not report["execution_attempted"] and not report["model_used"]
    assert report["files"][0]["changed_from_saved_build"]
    assert len(report["files"][0]["sha256"]) == 64
    assert len(service.manager.list()) == 1 and runtime.calls == [] and not marker.exists()


def test_reviewed_edit_runs_once_in_fresh_workspace_preserving_original(review_workspace):
    settings, service, workspace = review_workspace
    source = "# User reviewed this changed source\nprint('test fixture does not execute me')\n"
    (workspace / "generated/solution.py").write_text(source, encoding="utf-8")
    before = {str(path.relative_to(workspace)): path.read_bytes() for path in workspace.rglob("*") if path.is_file() and path.suffix != ".lock"}
    runtime = FixtureSandbox()
    report = debug_solution(settings, workspace.name, execute_reviewed_code=True, sandbox=runtime)
    assert report["status"] == "COMPLETED" and report["workspace_id"] != workspace.name
    assert len(runtime.calls) == 1 and runtime.calls[0][2] == 0
    clone, build, _ = runtime.calls[0]
    assert build.files[0].content.encode("utf-8") == (workspace / "generated/solution.py").read_bytes()
    assert len(build.validation_checks) == 1 and build.validation_checks[0].name == "Total JSON"
    assert (clone / "generated/solution.py").read_text(encoding="utf-8") == source
    assert (clone / "input/source.csv").read_bytes() == (workspace / "input/source.csv").read_bytes()
    provenance = service.manager.read(clone, "metadata/debug_provenance.json")
    assert provenance["manual_approved_code"] and provenance["original_workspace_id"] == workspace.name
    assert "execution_attempted" not in provenance and "execution_performed" not in provenance
    assert service.manager.read(clone, "metadata/job.json")["state"] == "finished"
    assert service.manager.read(clone, "metadata/manifest.json")["status"] == "COMPLETED"
    assert service.manager.read(clone, "metadata/attempts/0/execution.json")["success"]
    after = {str(path.relative_to(workspace)): path.read_bytes() for path in workspace.rglob("*") if path.is_file() and path.suffix != ".lock"}
    assert before == after


@pytest.mark.parametrize("lock_name", ["dispatch.lock", "workflow.lock"])
def test_original_locks_block_review_and_execution(review_workspace, lock_name):
    settings, service, workspace = review_workspace
    with FileLock(str(workspace / "metadata" / lock_name), timeout=0):
        with pytest.raises(ValueError, match="locked"):
            debug_solution(settings, workspace.name, execute_reviewed_code=True)
    assert len(service.manager.list()) == 1


def test_active_worker_is_rejected_before_clone(review_workspace):
    settings, service, workspace = review_workspace
    service.manager.write(workspace, "metadata/job.json", {"state": "running", "pid": os.getpid()})
    with pytest.raises(ValueError, match="active job"):
        debug_solution(settings, workspace.name, execute_reviewed_code=True)
    assert len(service.manager.list()) == 1


def test_unconfirmed_container_is_rejected_before_clone(review_workspace):
    settings, service, workspace = review_workspace
    service.manager.write(workspace, "metadata/sandbox.json", {"container_name": "adw-orphan", "cleanup_failed": True})
    with pytest.raises(ValueError, match="cleanup"):
        debug_solution(settings, workspace.name, execute_reviewed_code=True)
    assert len(service.manager.list()) == 1


def test_unknown_source_and_path_escape_are_rejected(review_workspace):
    settings, service, workspace = review_workspace
    unknown = workspace / "generated/extra.py"
    unknown.write_text("pass", encoding="utf-8")
    with pytest.raises(ValueError, match="unlisted"):
        debug_solution(settings, workspace.name, execute_reviewed_code=True)
    unknown.unlink()
    saved = service.manager.read(workspace, "metadata/build.json")
    saved["files"].append({"path": "generated/../../escape.py", "content": "pass"})
    service.manager.write(workspace, "metadata/build.json", saved)
    with pytest.raises(ValueError, match="safe workspace-relative"):
        debug_solution(settings, workspace.name, execute_reviewed_code=True)
    assert len(service.manager.list()) == 1


def test_changed_output_contract_and_oversized_source_are_rejected(review_workspace):
    settings, service, workspace = review_workspace
    saved = service.manager.read(workspace, "metadata/build.json")
    changed = json.loads(json.dumps(saved))
    changed["expected_outputs"][0]["location"] = "results/changed.json"
    service.manager.write(workspace, "metadata/build.json", changed)
    with pytest.raises(ValueError, match="approved contract"):
        debug_solution(settings, workspace.name, execute_reviewed_code=True)
    service.manager.write(workspace, "metadata/build.json", saved)
    (workspace / "generated/solution.py").write_bytes(b"#" * 256_001)
    with pytest.raises(ValueError, match="size limit"):
        debug_solution(settings, workspace.name, execute_reviewed_code=True)
    assert len(service.manager.list()) == 1


def test_failed_manual_attempt_persists_logs_evidence_and_status(review_workspace):
    settings, service, workspace = review_workspace
    runtime = FixtureSandbox(fail=True)
    report = debug_solution(settings, workspace.name, execute_reviewed_code=True, sandbox=runtime)
    assert report["status"] == "FAILED" and len(runtime.calls) == 1
    clone = service.manager.get(report["workspace_id"])
    assert service.manager.read(clone, "metadata/execution.json")["exit_code"] == 1
    assert not service.manager.read(clone, "metadata/attempts/0/validation.json")["passed"]
    assert service.manager.read(clone, "metadata/manifest.json")["status"] == "FAILED"
    assert (clone / "logs/attempt-0.stderr.log").read_text() == "fixture failure"


def test_changed_original_input_is_not_silently_cloned(review_workspace):
    settings, service, workspace = review_workspace
    (workspace / "input/source.csv").write_bytes(b"id,amount\n1,999\n")
    with pytest.raises(ValueError, match="ingestion fingerprint"):
        debug_solution(settings, workspace.name, execute_reviewed_code=True)
    assert len(service.manager.list()) == 1


def test_docker_cli_failure_is_reported_as_attempt_not_proof_code_ran(review_workspace):
    settings, service, workspace = review_workspace
    class UnavailableRuntime:
        def execute(self, workspace, build, attempt):
            return ExecutionResult(success=False, exit_code=125, execution_time_seconds=0,
                                   stdout_path="logs/attempt-0.stdout.log", stderr_path="logs/attempt-0.stderr.log",
                                   error="Docker could not start the container")
    report = debug_solution(settings, workspace.name, execute_reviewed_code=True, sandbox=UnavailableRuntime())
    assert report["execution_attempted"] and "execution_performed" not in report
    assert report["status"] == "FAILED" and not report["execution"]["success"]
    assert report["execution"]["exit_code"] == 125


@pytest.mark.docker
def test_real_docker_executes_edited_snapshot_not_old_code_or_outputs(review_workspace):
    settings, service, workspace = review_workspace
    source = ("import duckdb, json\nfrom pathlib import Path\n"
              "with duckdb.connect('database/workspace.duckdb') as con:\n"
              "    total = con.execute('SELECT sum(amount) FROM raw.source').fetchone()[0]\n"
              "Path('results/total.json').write_text(json.dumps({'total':total,'manual_edit':True}))\n")
    (workspace / "generated/solution.py").write_text(source, encoding="utf-8")
    report = debug_solution(settings, workspace.name, execute_reviewed_code=True)
    assert report["status"] == "COMPLETED", report
    clone = service.manager.get(report["workspace_id"])
    assert json.loads((clone / "results/total.json").read_text()) == {"total": 30, "manual_edit": True}
    assert json.loads((workspace / "results/total.json").read_text()) == {"total": 999}
    assert (clone / "notebooks/solution.ipynb").exists()
