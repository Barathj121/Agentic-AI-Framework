"""Persistent background job entrypoint; execution of generated code remains in Docker."""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

from filelock import FileLock

from workbench.settings import Settings
from workbench.workspace import WorkspaceManager, now, read_json, safe_path


def run(request_path: Path) -> int:
    request = read_json(request_path)
    settings = Settings(**request["settings"])
    manager = WorkspaceManager(settings.workspace_root)
    workspace = manager.get(request_path.parent.parent.name)
    if request_path.resolve() != safe_path(workspace, "metadata/" + request_path.name):
        raise ValueError("Job request outside workspace")
    with FileLock(str(workspace / "metadata/dispatch.lock"), timeout=30):
        job = manager.read(workspace, "metadata/job.json", {})
        if job.get("token") != request["token"]:
            raise ValueError("Job was superseded")
    exit_code = 0
    try:
        from workbench.workflow import Workflow, WorkflowCancelled
        workflow = Workflow(settings)
        action = request["action"]
        if action not in {"propose", "approve", "reject"}:
            raise ValueError("Unknown workflow action")
        arguments = dict(request["arguments"])
        if action == "propose":
            arguments["reset_cancellation"] = False
        outcome = getattr(workflow, action)(workspace, **arguments)
        if isinstance(outcome, dict) and outcome.get("status") == "FAILED":
            exit_code = 1
    except Exception as error:
        exit_code = 1
        cancelled = (workspace / "metadata/cancel.requested").exists()
        current = manager.read(workspace, "metadata/workspace.json", {})
        # Graph-node failures are already persisted by Workflow._invoke. A rejected
        # job precondition must not turn a completed workspace into a failed one.
        status = "CANCELLED" if cancelled else current.get("status", "FAILED")
        manager.status(workspace, status, last_job_error=str(error)[:4000])
        manager.event(workspace, "CANCELLED" if cancelled else "JOB_FAILED", "Worker cancelled" if cancelled else "Job could not start", error=str(error)[:4000])
        traceback.print_exc()
    finally:
        with FileLock(str(workspace / "metadata/dispatch.lock"), timeout=5):
            job = manager.read(workspace, "metadata/job.json", {})
            if job.get("token") == request["token"]:
                manager.write(workspace, "metadata/job.json", {**job, "state":"finished", "exit_code":exit_code, "finished_at":now()})
        request_path.unlink(missing_ok=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run(Path(sys.argv[1]).resolve()))
