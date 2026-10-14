from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from filelock import FileLock


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_path(workspace: Path, relative: str) -> Path:
    root = Path(workspace).resolve()
    text = str(relative).replace("\\", "/")
    parts = text.split("/")
    if not text or text.startswith("/") or PureWindowsPath(text).drive or any(p in {"..", ""} for p in parts) or ":" in text or "\x00" in text:
        raise ValueError("Unsafe workspace path")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Workspace links are not allowed")
    resolved = current.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Path leaves workspace")
    return resolved


def read_json(path: Path, default=None):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default


def atomic_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(obj, stream, indent=2, ensure_ascii=False, default=str, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class WorkspaceManager:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, name: str, problem: str) -> Path:
        if not name.strip() or not problem.strip():
            raise ValueError("Workspace name and problem are required")
        if len(name) > 200 or len(problem) > 12_000:
            raise ValueError("Name or problem is too long")
        identifier = f"uc_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:10]}"
        workspace = self.root / identifier
        for folder in ("input", "database", "generated", "notebooks", "models", "results", "logs", "metadata"):
            (workspace / folder).mkdir(parents=True)
        metadata = {"id": identifier, "workspace_id": identifier, "name": name.strip(), "problem": problem.strip(), "status": "CREATED", "created_at": now(), "updated_at": now()}
        self.write(workspace, "metadata/workspace.json", metadata)
        self.write(workspace, "metadata/manifest.json", {"workspace_id": identifier, "status": "CREATED", "artifacts": []})
        self.event(workspace, "CREATED", "Workspace created")
        return workspace

    def get(self, workspace_id: str) -> Path:
        if not re.fullmatch(r"uc_[A-Za-z0-9_]+", workspace_id):
            raise ValueError("Invalid workspace identifier")
        path = safe_path(self.root, workspace_id)
        if not path.is_dir() or not (path / "metadata/workspace.json").is_file():
            raise FileNotFoundError("Workspace not found")
        return path

    def _owned(self, workspace: Path) -> Path:
        workspace = Path(workspace)
        if workspace.parent.resolve() != self.root or workspace.is_symlink():
            raise ValueError("Workspace belongs to a different root")
        return self.get(workspace.name)

    def list(self) -> list[dict]:
        results = []
        for path in self.root.glob("uc_*"):
            try:
                results.append(read_json(self.get(path.name) / "metadata/workspace.json"))
            except (ValueError, OSError, json.JSONDecodeError):
                continue
        return sorted(results, key=lambda x: x.get("created_at", ""), reverse=True)

    def read(self, workspace: Path, relative: str, default=None):
        return read_json(safe_path(self._owned(workspace), relative), default)

    def write(self, workspace: Path, relative: str, obj) -> None:
        workspace = Path(workspace)
        if workspace.parent.resolve() != self.root or workspace.is_symlink():
            raise ValueError("Workspace belongs to a different root")
        atomic_json(safe_path(workspace, relative), obj)

    def event(self, workspace: Path, stage: str, message: str, **details) -> None:
        workspace = self._owned(workspace)
        event = {"timestamp": now(), "stage": stage, "message": str(message)[:4000], "details": details}
        with FileLock(str(workspace / "metadata/events.lock"), timeout=5):
            with safe_path(workspace, "logs/agent.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")

    def status(self, workspace: Path, status: str, **updates) -> None:
        with FileLock(str(workspace / "metadata/status.lock"), timeout=5):
            record = self.read(workspace, "metadata/workspace.json")
            record.update(updates, status=status, updated_at=now())
            self.write(workspace, "metadata/workspace.json", record)
            manifest = self.read(workspace, "metadata/manifest.json", {"workspace_id": workspace.name, "artifacts": []})
            manifest["status"] = status
            self.write(workspace, "metadata/manifest.json", manifest)
