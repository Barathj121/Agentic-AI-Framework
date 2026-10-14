from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from filelock import FileLock

from workbench.data import ingest_csv
from workbench.settings import Settings
from workbench.workspace import WorkspaceManager, atomic_json, now, read_json, safe_path


def process_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Service:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.manager = WorkspaceManager(self.settings.workspace_root)

    def create_workspace(self, name: str, problem: str, content: bytes, filename: str) -> Path:
        workspace = self.manager.create(name, problem)
        ingest_csv(workspace, content, filename, self.settings)
        return workspace

    def _dispatch(self, workspace_id: str, action: str, **arguments):
        workspace = self.manager.get(workspace_id)
        with FileLock(str(workspace / "metadata/dispatch.lock"), timeout=1):
            existing = self.manager.read(workspace, "metadata/job.json", {})
            if existing.get("state") == "running" and process_alive(existing.get("pid", 0)):
                raise ValueError("This workspace already has a running job")
            if action == "propose":
                safe_path(workspace, "metadata/cancel.requested").unlink(missing_ok=True)
            token = uuid.uuid4().hex
            request = {"action": action, "arguments": arguments, "token": token,
                       "settings": {key:str(value) if isinstance(value, Path) else value for key,value in asdict(self.settings).items()
                                    if key != "llm_api_key"}}
            request_path = safe_path(workspace, f"metadata/request_{token}.json")
            atomic_json(request_path, request)
            output_path = workspace / "logs/worker.log"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            environment = os.environ.copy()
            environment.update(PYTHONUNBUFFERED="1", LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
            # Credentials remain in the control-plane child environment, never
            # workspace request files, events or the Docker execution contract.
            environment["LLM_API_KEY"] = self.settings.llm_api_key
            # Hold dispatch lock until PID is registered; the worker waits on this lock first.
            with output_path.open("a", encoding="utf-8") as log:
                child = subprocess.Popen([sys.executable, "-m", "workbench.worker", str(request_path)],
                    cwd=Path(__file__).resolve().parents[1], env=environment,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=flags, start_new_session=os.name != "nt")
            self.manager.write(workspace, "metadata/job.json", {"token":token, "pid":child.pid, "state":"running", "action":action, "started_at":now()})
            self.manager.event(workspace, "QUEUED", f"Started {action} worker", pid=child.pid)

    def start_proposals(self, workspace_id: str):
        status = self.status(workspace_id)
        if status.get("worker_active") or status["status"] == "COMPLETED":
            raise ValueError("This workspace is running or completed; use a new workspace for another solution")
        if status["status"] == "AWAITING_APPROVAL":
            return
        self._dispatch(workspace_id, "propose")

    def approve(self, workspace_id: str, proposal_id: str, instructions: str = ""):
        if len(instructions) > 4000:
            raise ValueError("Additional instructions must be at most 4,000 characters")
        workspace = self.manager.get(workspace_id)
        status = self.manager.read(workspace, "metadata/workspace.json").get("status")
        proposals = self.manager.read(workspace, "metadata/proposals.json", {}).get("proposals", [])
        if status != "AWAITING_APPROVAL":
            raise ValueError("The workflow is not awaiting approval")
        selected = next((p for p in proposals if p.get("proposal_id") == proposal_id), None)
        if selected is None or selected.get("feasibility") == "NOT_FEASIBLE":
            raise ValueError("Select an available feasible proposal")
        self._dispatch(workspace_id, "approve", proposal_id=proposal_id, instructions=instructions)

    def reject(self, workspace_id: str, reason: str = ""):
        if self.status(workspace_id)["status"] != "AWAITING_APPROVAL":
            raise ValueError("The workflow is not awaiting approval")
        self._dispatch(workspace_id, "reject", reason=reason[:8000])

    def cancel(self, workspace_id: str):
        from workbench.workflow import Workflow
        Workflow(self.settings).cancel(self.manager.get(workspace_id))

    def status(self, workspace_id: str) -> dict:
        workspace = self.manager.get(workspace_id)
        metadata = self.manager.read(workspace, "metadata/workspace.json")
        job = self.manager.read(workspace, "metadata/job.json", {})
        active = job.get("state") == "running" and process_alive(job.get("pid", 0))
        if job.get("state") == "running" and not active:
            # A hard kill cannot execute finally; expose interruption without inventing completion.
            metadata = {**metadata, "worker_interrupted": True, "error": "The worker stopped before recording completion. Saved files and logs remain available."}
            if metadata["status"] not in {"COMPLETED", "FAILED", "CANCELLED", "REJECTED", "AWAITING_APPROVAL"}:
                metadata["status"] = "INTERRUPTED"
        if active and metadata["status"] in {"CREATED", "DATA_PROFILED", "AWAITING_APPROVAL"}:
            metadata["status"] = "QUEUED"
        return {**metadata, "worker_active": active, "job": job}

    def events(self, workspace_id: str) -> list[dict]:
        path = self.manager.get(workspace_id) / "logs/agent.jsonl"
        if not path.exists():
            return []
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 256_000))
            content = stream.read().decode("utf-8", errors="replace")
        events = []
        for line in content.splitlines()[-300:]:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def diagnostics(self) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        from workbench.llm import create_llm
        from workbench.sandbox import SandboxManager
        client = create_llm(self.settings)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                model = pool.submit(client.health)
                docker = pool.submit(SandboxManager(self.settings).health)
                health = model.result()
                return {"python": sys.version.split()[0], "workspace_root":str(self.settings.workspace_root),
                        "provider":self.settings.llm_provider, "model":client.model,
                        "llm":health, "ollama":health, "docker":docker.result()}
        finally:
            client.close()
