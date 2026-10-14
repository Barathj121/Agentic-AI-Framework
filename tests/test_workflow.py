"""Workflow contract tests. Fixture sandbox never executes model-generated code."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from workbench.data import ingest_csv
from workbench.schemas import BuildResult, ExecutionResult, Proposal, ProposalSet, Understanding
from workbench.settings import Settings
from workbench.workflow import Workflow
from workbench.workspace import WorkspaceManager


def proposal(**updates):
    data = {
        "proposal_id": "aggregate", "title": "Aggregate measured sales", "description": "Compute total sales",
        "approach": "Deterministic aggregation", "implementation_steps": ["Sum amount", "Save a JSON report"],
        "required_columns": ["amount"],
        "expected_outputs": [{"name": "Computed total", "type": "json", "location": "results/total.json"}],
        "advantages": ["Simple"], "limitations": ["Descriptive, not predictive"], "assumptions": [],
        "resource_estimate": {"cpu": "low", "memory": "low", "execution_time": "short"},
        "validation_strategy": ["Check output exists and is JSON"], "feasibility": "FEASIBLE",
        "confidence_explanation": "The supplied column supports aggregation", "required_dependencies": ["duckdb"],
    }
    data.update(updates)
    return Proposal.model_validate(data)


def build(plan=None, code=None):
    plan = plan or proposal()
    return BuildResult.model_validate({
        "files": [{"path": "generated/solution.py", "content": code or
                   "import json\nimport duckdb\nfrom pathlib import Path\n"
                   "def main():\n"
                   "    with duckdb.connect('database/workspace.duckdb') as con:\n"
                   "        value = con.execute('SELECT sum(amount) FROM raw.source').fetchone()[0]\n"
                   "    Path('results/total.json').write_text(json.dumps({'total': value}))\n"
                   "if __name__ == '__main__':\n    main()\n"}],
        "required_dependencies": ["duckdb"], "expected_outputs": [o.model_dump() for o in plan.expected_outputs],
        "validation_checks": [{"name": "Real JSON output", "kind": "json_valid", "target": "results/total.json"}],
    })


class FixtureLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def structured(self, schema_class, system, prompt):
        self.calls.append((schema_class, system, prompt))
        value = self.responses.pop(0)
        return schema_class.model_validate(value)

    def health(self):
        return {"available": True, "model_available": True}


class FixtureSandbox:
    """Explicit test double: writes fixture output, never evaluates generated source."""
    def __init__(self, fail_attempts=0, write_output=True):
        self.fail_attempts = fail_attempts
        self.write_output = write_output
        self.calls = []
        self.cancelled = False

    def execute(self, workspace, build, attempt):
        self.calls.append(attempt)
        failed = attempt < self.fail_attempts
        stdout = f"logs/stdout-{attempt}.log"
        stderr = f"logs/stderr-{attempt}.log"
        (workspace / stdout).write_text("Explicit fixture execution evidence", encoding="utf-8")
        (workspace / stderr).write_text("Fixture computation error" if failed else "", encoding="utf-8")
        if not failed and self.write_output:
            (workspace / "results/total.json").write_text('{"total": 30, "source": "test fixture"}', encoding="utf-8")
        return ExecutionResult(success=not failed, exit_code=1 if failed else 0,
                               execution_time_seconds=0.1, stdout_path=stdout, stderr_path=stderr,
                               error="Fixture error" if failed else None)

    def inspect(self, workspace, checks):
        return {"checks": [], "artifacts": []}

    def cancel(self, workspace):
        self.cancelled = True


@pytest.fixture
def prepared(tmp_path):
    settings = Settings(workspace_root=tmp_path / "workspaces")
    manager = WorkspaceManager(settings.workspace_root)
    workspace = manager.create("Sales fixture", "Compute total sales amount")
    ingest_csv(workspace, b"item,amount\nA,10\nB,20\n", "sales.csv", settings)
    return settings, manager, workspace


def planning_responses(plan=None, queries=None):
    return [Understanding(summary="Sum measured amounts", relevant_columns=["amount"], exploration_queries=queries or []),
            ProposalSet(proposals=[plan or proposal()])]


def test_durable_approval_is_required_before_any_execution(prepared):
    settings, manager, workspace = prepared
    sandbox = FixtureSandbox()
    first = Workflow(settings, llm=FixtureLLM(planning_responses()), sandbox=sandbox)
    state = first.propose(workspace)
    assert state["status"] == "AWAITING_APPROVAL"
    assert sandbox.calls == []
    assert not (workspace / "generated/solution.py").exists()
    assert (workspace / "metadata/checkpoints.sqlite").stat().st_size > 0
    # A fresh process-equivalent instance must resume the persisted graph, with
    # no dependency on the first object's in-memory state or a Streamlit session.
    second = Workflow(settings, llm=FixtureLLM([build()]), sandbox=sandbox)
    state = second.approve(workspace, "aggregate", "Retain the measured units")
    assert state["status"] == "COMPLETED", state.get("error")
    assert sandbox.calls == [0]
    plan = manager.read(workspace, "metadata/selected_plan.json")
    assert plan["instructions"] == "Retain the measured units"
    manifest = manager.read(workspace, "metadata/manifest.json")
    locations = {a["location"] for a in manifest["artifacts"]}
    assert {"results/total.json", "notebooks/solution.ipynb", "generated/solution.py"}.issubset(locations)
    assert manager.read(workspace, "metadata/validation.json")["passed"] is True
    notebook = json.loads((workspace / "notebooks/solution.ipynb").read_text(encoding="utf-8"))
    assert notebook["metadata"]["workbench"]["executed"] is False
    with pytest.raises(ValueError, match="not awaiting"):
        second.approve(workspace, "aggregate")


def test_missing_columns_unavailable_dependencies_and_resources_block_approval(prepared):
    settings, manager, workspace = prepared
    plan = proposal(required_columns=["absent_label"], required_dependencies=["uninstalled-special-library"],
                    resource_estimate={"cpu": "8 cores", "memory": "16GB", "execution_time": "20 minutes"})
    sandbox = FixtureSandbox()
    workflow = Workflow(settings, llm=FixtureLLM(planning_responses(plan)), sandbox=sandbox)
    state = workflow.propose(workspace)
    candidate = state["proposals"][0]
    assert candidate["feasibility"] == "NOT_FEASIBLE"
    assert len(manager.read(workspace, "metadata/feasibility.json")["checks"][0]["errors"]) == 5
    with pytest.raises(ValueError, match="NOT_FEASIBLE"):
        workflow.approve(workspace, "aggregate")
    assert not sandbox.calls


def test_rejection_persists_without_execution_and_new_proposals_are_possible(prepared):
    settings, manager, workspace = prepared
    sandbox = FixtureSandbox()
    workflow = Workflow(settings, llm=FixtureLLM(planning_responses()), sandbox=sandbox)
    workflow.propose(workspace)
    first_run = manager.read(workspace, "metadata/workflow.json")["run_id"]
    rejected = Workflow(settings, llm=FixtureLLM([]), sandbox=sandbox).reject(workspace, "Need a different approach")
    assert rejected["status"] == "REJECTED"
    assert manager.read(workspace, "metadata/decision.json")["reason"] == "Need a different approach"
    workflow.llm = FixtureLLM(planning_responses())
    assert workflow.propose(workspace)["status"] == "AWAITING_APPROVAL"
    assert manager.read(workspace, "metadata/workflow.json")["run_id"] != first_run
    assert not sandbox.calls


def test_cancel_pending_approval_is_durable(prepared):
    settings, manager, workspace = prepared
    sandbox = FixtureSandbox()
    workflow = Workflow(settings, llm=FixtureLLM(planning_responses()), sandbox=sandbox)
    workflow.propose(workspace)
    workflow.cancel(workspace)
    reopened = Workflow(settings, llm=FixtureLLM([]), sandbox=sandbox)
    assert reopened.snapshot(workspace)["status"] == "CANCELLED"
    assert manager.read(workspace, "metadata/workspace.json")["status"] == "CANCELLED"
    assert sandbox.cancelled
    with pytest.raises(RuntimeError, match="Cancelled"):
        reopened.approve(workspace, "aggregate")


def test_cancel_does_not_wait_for_active_workflow_lock(prepared):
    settings, manager, workspace = prepared
    entered, release = threading.Event(), threading.Event()

    class BlockingFixtureLLM(FixtureLLM):
        def structured(self, schema_class, system, prompt):
            entered.set()
            assert release.wait(timeout=10)
            return super().structured(schema_class, system, prompt)

    sandbox = FixtureSandbox()
    workflow = Workflow(settings, llm=BlockingFixtureLLM(planning_responses()), sandbox=sandbox)
    result = []
    thread = threading.Thread(target=lambda: result.append(workflow.propose(workspace)))
    thread.start()
    try:
        assert entered.wait(timeout=10)
        workflow.cancel(workspace)
        assert (workspace / "metadata/cancel.requested").exists()
        assert sandbox.cancelled
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert result[0]["status"] == "CANCELLED"
    assert not sandbox.calls


def test_actual_failure_evidence_drives_bounded_repair(prepared):
    settings, manager, workspace = prepared
    sandbox = FixtureSandbox(fail_attempts=1)
    llm = FixtureLLM([*planning_responses(), build(), build()])
    workflow = Workflow(settings, llm=llm, sandbox=sandbox)
    workflow.propose(workspace)
    result = workflow.approve(workspace, "aggregate")
    assert result["status"] == "COMPLETED", result.get("error")
    assert sandbox.calls == [0, 1]
    assert "Fixture computation error" in llm.calls[-1][2]
    assert manager.read(workspace, "metadata/attempts/0/execution.json")["success"] is False
    assert manager.read(workspace, "metadata/attempts/1/execution.json")["success"] is True


def test_repair_limit_preserves_failed_attempts(prepared):
    settings, manager, workspace = prepared
    sandbox = FixtureSandbox(fail_attempts=100)
    workflow = Workflow(settings, llm=FixtureLLM([*planning_responses(), build(), build(), build()]), sandbox=sandbox)
    workflow.propose(workspace)
    result = workflow.approve(workspace, "aggregate")
    assert result["status"] == "FAILED"
    assert sandbox.calls == [0, 1, 2]
    assert len(result["history"]) == 3
    assert (workspace / "generated/solution.py").exists()
    assert (workspace / "metadata/attempts/2/build.json").exists()


def test_exit_zero_without_real_output_is_never_completed(prepared):
    settings, manager, workspace = prepared
    sandbox = FixtureSandbox(write_output=False)
    workflow = Workflow(settings, llm=FixtureLLM([*planning_responses(), build(), build(), build()]), sandbox=sandbox)
    workflow.propose(workspace)
    result = workflow.approve(workspace, "aggregate")
    assert result["status"] == "FAILED"
    assert sandbox.calls == [0, 1, 2]
    assert result["execution"]["exit_code"] == 0
    assert manager.read(workspace, "metadata/validation.json")["passed"] is False
    assert "does not exist" in result["error"]


def test_builder_cannot_change_approved_output_contract(prepared):
    settings, _, workspace = prepared
    changed = proposal(expected_outputs=[{"name": "Different result", "type": "json", "location": "results/other.json"}])
    sandbox = FixtureSandbox()
    workflow = Workflow(settings, llm=FixtureLLM([*planning_responses(), build(changed), build(changed), build(changed)]), sandbox=sandbox)
    workflow.propose(workspace)
    result = workflow.approve(workspace, "aggregate")
    assert result["status"] == "FAILED"
    assert "approved contract" in result["error"]
    assert not sandbox.calls


def test_exploration_is_read_only_and_errors_become_evidence(prepared):
    settings, manager, workspace = prepared
    queries = ["SELECT sum(amount) AS total FROM raw.source", "DROP TABLE raw.source"]
    workflow = Workflow(settings, llm=FixtureLLM(planning_responses(queries=queries)), sandbox=FixtureSandbox())
    state = workflow.propose(workspace)
    assert state["status"] == "AWAITING_APPROVAL"
    evidence = manager.read(workspace, "metadata/exploration.json")
    assert "error" not in evidence[0]
    assert "error" in evidence[1]


def test_workflow_disables_ambient_cloud_tracing(prepared, monkeypatch):
    import os
    settings, _, _ = prepared
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    Workflow(settings, llm=FixtureLLM([]), sandbox=FixtureSandbox())
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"


def test_understanding_cannot_invent_relevant_columns(prepared):
    settings, _, workspace = prepared
    llm = FixtureLLM([Understanding(summary="Invented label", relevant_columns=["fraud_label"])])
    workflow = Workflow(settings, llm=llm, sandbox=FixtureSandbox())
    result = workflow.propose(workspace)
    assert result["status"] == "FAILED"
    assert "absent from the actual schema" in result["error"]
    assert len(llm.calls) == 1


def test_exploration_masks_strings_even_if_identifiers_are_aliased(prepared):
    settings, manager, workspace = prepared
    queries = ["SELECT item AS harmless_alias FROM raw.source"]
    workflow = Workflow(settings, llm=FixtureLLM(planning_responses(queries=queries)), sandbox=FixtureSandbox())
    workflow.propose(workspace)
    evidence = manager.read(workspace, "metadata/exploration.json")
    assert evidence[0]["rows"][0][0] == "<text length=1>"


def test_syntax_error_repaired_before_any_execution(prepared):
    settings, manager, workspace = prepared
    sandbox = FixtureSandbox()
    llm = FixtureLLM([*planning_responses(), build(code="def incomplete(\n"), build()])
    workflow = Workflow(settings, llm=llm, sandbox=sandbox)
    workflow.propose(workspace)
    result = workflow.approve(workspace, "aggregate")
    assert result["status"] == "COMPLETED", result.get("error")
    assert sandbox.calls == [1]
    assert "SyntaxError" in llm.calls[-1][2]
    assert manager.read(workspace, "metadata/attempts/0/generation_error.json")["error"].startswith("SyntaxError")


def test_queued_proposal_cancellation_is_not_reset_by_worker(prepared):
    settings, _, workspace = prepared
    workflow = Workflow(settings, llm=FixtureLLM([]), sandbox=FixtureSandbox())
    workflow.cancel(workspace)
    state = workflow.propose(workspace, reset_cancellation=False)
    assert state["status"] == "CANCELLED"
    assert workflow.llm.calls == []


def test_missing_docker_fails_without_wasting_code_repair_calls(prepared):
    settings, _, workspace = prepared

    class UnavailableSandbox(FixtureSandbox):
        def execute(self, workspace, build, attempt):
            self.calls.append(attempt)
            return ExecutionResult(success=False, exit_code=None, execution_time_seconds=0,
                                   stdout_path="logs/not-started.log", stderr_path="logs/not-started.err",
                                   error="Docker engine is unavailable")

    sandbox = UnavailableSandbox()
    workflow = Workflow(settings, llm=FixtureLLM([*planning_responses(), build()]), sandbox=sandbox)
    workflow.propose(workspace)
    result = workflow.approve(workspace, "aggregate")
    assert result["status"] == "FAILED"
    assert "Docker engine" in result["error"]
    assert sandbox.calls == [0]
    assert len(workflow.llm.calls) == 3
