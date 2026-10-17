"""Opt-in real local-model smoke test; execution requires separate explicit approval."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def verify_smoke_result(service, workspace: Path) -> dict:
    """Check actual smoke rows and retain both generic and independent evidence.

    This is a post-run benchmark, not a replacement for the ADK workflow history.
    It never runs generated code, calls a model, or changes a checkpoint. A
    failed comparison overrides display status so known wrong results cannot
    retain an overall COMPLETED label.
    """
    from filelock import FileLock
    from workbench.query import sql_preview
    from workbench.workspace import now, safe_path

    manager = service.manager
    workspace = Path(workspace)
    owned = manager.get(workspace.name)
    if workspace.resolve() != owned.resolve():
        raise ValueError("Smoke verification workspace is outside the configured root")
    workspace = owned
    expected = [["C1", 25], ["C2", 45], ["C3", 65]]
    query = "SELECT customer_id, total_amount FROM results.customer_totals ORDER BY customer_id"
    with FileLock(str(safe_path(workspace, "metadata/workflow.lock")), timeout=0):
        independent_check = {"passed": False, "expected": expected, "actual": None,
                             "query": query, "checked_at": now(), "truncated": False, "error": None}
        try:
            # Four rows are sufficient to detect extra rows against the exact
            # three-row expectation; the query layer also enforces time/memory bounds.
            actual = sql_preview(workspace, query, limit=4)
            independent_check.update(actual=actual["rows"], truncated=bool(actual.get("truncated")))
            independent_check["passed"] = actual["rows"] == expected and not actual.get("truncated", False)
            if not independent_check["passed"]:
                independent_check["error"] = (
                    "Exact customer totals differ: expected C1=25, C2=45, C3=65 with exactly three rows."
                )
        except Exception as exc:
            independent_check["error"] = f"Could not query the required smoke result: {type(exc).__name__}: {exc}"[:2000]

        manager.write(workspace, "metadata/smoke_validation.json", independent_check)
        check = {"name": "Independent smoke benchmark: exact customer totals", "kind": "sql_assertion",
                 "target": "results.customer_totals", "passed": independent_check["passed"],
                 "evidence": independent_check}
        failure = None
        if not independent_check["passed"]:
            failure = "Independent smoke benchmark failed: " + independent_check["error"]
            check["error"] = failure

        validation = manager.read(workspace, "metadata/validation.json", {}) or {}
        validation.setdefault("generic_validation_passed", validation.get("passed") is True)
        validation["checks"] = [*validation.get("checks", []), check]
        validation["errors"] = list(validation.get("errors", []))
        if failure and failure not in validation["errors"]:
            validation["errors"].append(failure)
        validation["passed"] = validation.get("passed") is True and independent_check["passed"]
        validation["smoke_test_passed"] = independent_check["passed"]
        validation["independent_smoke_validation"] = independent_check
        manager.write(workspace, "metadata/validation.json", validation)

        manifest = manager.read(workspace, "metadata/manifest.json", {}) or {}
        manifest.update(smoke_test_passed=independent_check["passed"], validation_passed=validation["passed"],
                        independent_smoke_validation=independent_check)
        if failure:
            manifest["postrun_benchmark_failure"] = failure
        for artifact in manifest.get("artifacts", []):
            if artifact.get("location") == "results.customer_totals":
                artifact["smoke_test_passed"] = independent_check["passed"]
                if failure:
                    artifact["validation_status"] = "FAILED"
        manager.write(workspace, "metadata/manifest.json", manifest)

        metadata = manager.read(workspace, "metadata/workspace.json", {})
        if failure:
            manager.status(workspace, "FAILED", error=failure, smoke_test_passed=False,
                           failure_stage="INDEPENDENT_SMOKE_VALIDATION")
        else:
            # A passing row check cannot erase an earlier execution/validation failure.
            manager.status(workspace, metadata.get("status", "FAILED"), smoke_test_passed=True)
        manager.event(workspace, "INDEPENDENT_SMOKE_VALIDATION",
                      "Exact smoke customer totals verified" if not failure else failure,
                      passed=independent_check["passed"])
        return independent_check


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-model", "--real-ollama", dest="real_model", action="store_true", help="Required: run the configured real local model (Ollama or a local compatible API).")
    parser.add_argument("--workspace", help="Existing smoke workspace ID to approve after reviewing its proposals.")
    parser.add_argument("--approve", metavar="PROPOSAL_ID", help="Explicitly approve this proposal in --workspace and execute in Docker.")
    args = parser.parse_args()
    if not args.real_model:
        parser.error("Pass --real-model to opt in to live local inference. The standard tests use deterministic test adapters.")
    if bool(args.workspace) != bool(args.approve):
        parser.error("Use --workspace and --approve together, after reviewing the generated proposals.")

    from workbench.service import Service
    from workbench.settings import Settings
    from workbench.workflow import Workflow

    settings = Settings.from_env()
    service = Service(settings)
    workflow = Workflow(settings)
    if args.workspace:
        workspace = service.manager.get(args.workspace)
        workflow.approve(workspace, args.approve)
    else:
        content = b"customer_id,amount\nC1,10\nC1,15\nC2,20\nC2,25\nC3,30\nC3,35\n"
        workspace = service.create_workspace("Real local-model smoke test",
            "Use SQL to sum amount by customer_id. Create results.customer_totals with customer_id and total_amount. "
            "Validate there are three rows and the grand total is 135. Save reusable SQL/Python and a notebook.",
            content, "smoke.csv")
        workflow.propose(workspace)
    metadata = service.status(workspace.name)
    if not args.workspace:
        print(json.dumps(metadata, indent=2, default=str))
        print(json.dumps(service.manager.read(workspace, "metadata/proposals.json", {}), indent=2))
        print("\nReview these proposals. To approve one and execute it in Docker, run:")
        print(f'python scripts/smoke_test.py --real-model --workspace "{workspace.name}" --approve "<proposal_id>"')
        return 0 if metadata.get("status") == "AWAITING_APPROVAL" else 1
    if metadata.get("status") != "COMPLETED":
        print(json.dumps(metadata, indent=2, default=str))
        print(json.dumps(service.manager.read(workspace, "metadata/validation.json", {}), indent=2))
        print(f"Persistent workspace: {workspace.resolve()}")
        return 1
    independent_check = verify_smoke_result(service, workspace)
    print(json.dumps(service.status(workspace.name), indent=2, default=str))
    print(json.dumps(service.manager.read(workspace, "metadata/validation.json", {}), indent=2, default=str))
    print(f"Persistent workspace: {workspace.resolve()}")
    print(json.dumps({"independent_smoke_validation": independent_check}, indent=2, default=str))
    return 0 if independent_check["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
