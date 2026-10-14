import os
import time

import pytest

from workbench.service import Service, process_alive
from workbench.settings import Settings


def test_background_worker_reports_provider_failure_and_releases_job(tmp_path):
    service = Service(Settings(workspace_root=tmp_path, ollama_base_url="http://127.0.0.1:1", llm_timeout_seconds=1))
    workspace = service.create_workspace("Worker persistence", "Total amount", b"id,amount\na,10\nb,20\n", "small.csv")
    service.start_proposals(workspace.name)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = service.manager.read(workspace, "metadata/job.json", {})
        if job.get("state") == "finished":
            break
        time.sleep(0.1)
    assert job["state"] == "finished"
    assert job["exit_code"] == 1
    fresh = Service(service.settings)
    assert fresh.status(workspace.name)["status"] == "FAILED"
    assert "Ollama" in fresh.status(workspace.name)["error"]
    assert any(event["stage"] == "FAILED" for event in fresh.events(workspace.name))
    assert not (workspace / "generated/solution.py").exists()


def test_approval_rejected_before_dispatch_without_state_corruption(tmp_path):
    service = Service(Settings(workspace_root=tmp_path))
    workspace = service.create_workspace("Approval", "Total", b"amount\n1\n", "small.csv")
    with pytest.raises(ValueError, match="awaiting"):
        service.approve(workspace.name, "invented")
    assert service.status(workspace.name)["status"] == "DATA_PROFILED"
    assert not (workspace / "metadata/job.json").exists()


def test_process_check_is_readonly():
    assert process_alive(os.getpid())
    assert not process_alive(-1)


def test_invalid_actions_do_not_corrupt_completed_workspace(tmp_path):
    service = Service(Settings(workspace_root=tmp_path))
    workspace = service.create_workspace("Complete", "Total", b"amount\n1\n", "small.csv")
    service.manager.status(workspace, "COMPLETED")
    with pytest.raises(ValueError):
        service.start_proposals(workspace.name)
    with pytest.raises(ValueError):
        service.reject(workspace.name)
    assert service.status(workspace.name)["status"] == "COMPLETED"


def test_interrupted_worker_is_recoverable(tmp_path):
    service = Service(Settings(workspace_root=tmp_path))
    workspace = service.create_workspace("Interrupted", "Total", b"amount\n1\n", "small.csv")
    service.manager.status(workspace, "EXECUTING")
    service.manager.write(workspace, "metadata/job.json", {"state": "running", "pid": -1})
    status = service.status(workspace.name)
    assert status["status"] == "INTERRUPTED"
    assert status["worker_interrupted"]
    assert not status["worker_active"]
