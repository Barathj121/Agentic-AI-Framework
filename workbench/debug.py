"""Review edited generated files and run an approved snapshot in a fresh sandbox workspace."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from filelock import FileLock, Timeout

from workbench.artifacts import validate_and_register
from workbench.sandbox import SandboxManager
from workbench.schemas import BuildResult, ExecutionResult
from workbench.service import Service, process_alive
from workbench.settings import Settings
from workbench.workflow import Workflow
from workbench.workspace import now, safe_path

MAX_SOURCE_BYTES = 256_000


class _NoModel:
    def structured(self, *args, **kwargs):
        raise RuntimeError("Manual debugging must never call a language model")

    def health(self):
        raise RuntimeError("Manual debugging does not require a language model")


def _read_bytes(path: Path, limit: int) -> bytes:
    if not path.is_file():
        raise ValueError(f"Required file is missing: {path.name}")
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError(f"File exceeds the review size limit: {path.name}")
    return value


def _snapshot(service: Service, original: Path) -> tuple[dict, BuildResult, bytes, dict]:
    manager = service.manager
    job = manager.read(original, "metadata/job.json", {})
    if job.get("state") == "running" and process_alive(job.get("pid", 0)):
        raise ValueError("The original workspace has an active job; wait for it to finish")
    sandbox_state = manager.read(original, "metadata/sandbox.json", {})
    if sandbox_state.get("container_name") or sandbox_state.get("cleanup_failed"):
        raise ValueError("The original workspace has an active or unconfirmed sandbox; resolve cleanup first")
    selected = json.loads(_read_bytes(safe_path(original, "metadata/selected_plan.json"), 1_000_000))
    saved = BuildResult.model_validate_json(_read_bytes(safe_path(original, "metadata/build.json"), 1_000_000))
    paths = {file.path for file in saved.files}
    if len(paths) > 30:
        raise ValueError("At most 30 generated files may be reviewed")
    found = set()
    visited = 0
    for current, directories, filenames in os.walk(safe_path(original, "generated"), followlinks=False):
        for name in [*directories, *filenames]:
            visited += 1
            if visited > 128:
                raise ValueError("Generated source tree exceeds the review inventory limit")
            relative = (Path(current) / name).relative_to(original).as_posix()
            path = safe_path(original, relative)  # Reject links and junctions before descending.
            if name in filenames:
                if not path.is_file():
                    raise ValueError("Only regular generated source files can be reviewed")
                found.add(relative)
    if found != paths:
        raise ValueError("Generated files differ from the saved build file list; restore missing files and remove unlisted files before reviewing")
    total = 0
    sources, evidence = [], []
    for file in saved.files:
        path = safe_path(original, file.path)
        if path.relative_to(original).as_posix() != file.path:
            raise ValueError("Generated source paths must use their canonical relative spelling")
        content = _read_bytes(path, MAX_SOURCE_BYTES - total)
        total += len(content)
        sources.append({"path": file.path, "content": content.decode("utf-8")})
        evidence.append({"path": file.path, "size_bytes": len(content),
                         "sha256": hashlib.sha256(content).hexdigest(),
                         "changed_from_saved_build": content != file.content.encode("utf-8")})
    updated = BuildResult.model_validate({**saved.model_dump(mode="json"), "files": sources})
    # Reuse the production contract gate without creating a model client or a graph run.
    checked = Workflow(service.settings, llm=_NoModel())._check_build(original, {"selected_plan": selected}, updated)
    if checked.validation_checks != saved.validation_checks:
        raise ValueError("Manual edits cannot change the saved validation checks")
    metadata = manager.read(original, "metadata/workspace.json")
    source = _read_bytes(safe_path(original, "input/source.csv"), service.settings.max_upload_mb * 1024 * 1024)
    profile = manager.read(original, "metadata/profile.json", {})
    source_hash = hashlib.sha256(source).hexdigest()
    if profile.get("source_sha256") != source_hash:
        raise ValueError("Original CSV no longer matches its ingestion fingerprint; restore it before debugging")
    report = {"original_workspace_id": original.name, "reviewed_at": now(),
              "files": evidence, "total_source_bytes": total,
              "input_sha256": source_hash,
              "expected_outputs": [item.model_dump(mode="json") for item in checked.expected_outputs],
              "validation_checks": [item.model_dump(mode="json") for item in checked.validation_checks]}
    context = {"selected_plan": selected, "metadata": metadata,
               "filename": profile.get("source_filename") or "source.csv"}
    return report, checked, source, context


def debug_solution(settings: Settings, workspace_id: str, *, execute_reviewed_code: bool = False,
                   sandbox=None) -> dict:
    """Default is review only. Explicit execution clones input and runs exactly one snapshot."""
    service = Service(settings)
    manager = service.manager
    original = manager.get(workspace_id)
    try:
        with FileLock(str(safe_path(original, "metadata/dispatch.lock")), timeout=0):
            with FileLock(str(safe_path(original, "metadata/workflow.lock")), timeout=0):
                report, build, source, context = _snapshot(service, original)
    except Timeout as exc:
        raise ValueError("The original workspace is locked by another operation; wait before debugging") from exc
    report.update(dry_run=not execute_reviewed_code, execution_requested=execute_reviewed_code,
                  execution_attempted=False, model_used=False)
    if not execute_reviewed_code:
        return report

    # Re-ingest the original CSV, never clone derived databases, output files, or checkpoints.
    workspace = service.create_workspace(("DEBUG · " + context["metadata"]["name"])[:200],
                                         context["metadata"]["problem"], source, context["filename"])
    report.update(workspace_id=workspace.name, workspace_path=str(workspace))
    token = uuid.uuid4().hex
    with FileLock(str(safe_path(workspace, "metadata/dispatch.lock")), timeout=0):
        with FileLock(str(safe_path(workspace, "metadata/workflow.lock")), timeout=0):
            manager.write(workspace, "metadata/job.json", {"token": token, "pid": os.getpid(),
                          "state": "running", "action": "manual_debug", "started_at": now()})
            try:
                provenance = {key: value for key, value in report.items()
                              if key not in {"dry_run", "execution_requested", "execution_attempted"}}
                provenance.update(manual_approved_code=True, approval="--execute-reviewed-code",
                                  source_policy="Exact snapshot of listed generated files; saved validation and approved outputs retained")
                manager.write(workspace, "metadata/debug_provenance.json", provenance)
                manager.write(workspace, "metadata/selected_plan.json", context["selected_plan"])
                manager.write(workspace, "metadata/proposals.json", {"proposals": [context["selected_plan"]["proposal"]]})
                manager.write(workspace, "metadata/build.json", build.model_dump(mode="json"))
                manager.write(workspace, "metadata/attempts/0/build.json", build.model_dump(mode="json"))
                for file in build.files:
                    path = safe_path(workspace, file.path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(file.content.encode("utf-8"))
                manager.status(workspace, "EXECUTING", error=None)
                manager.event(workspace, "EXECUTING", "Running explicitly reviewed code snapshot in Docker; no model calls", original_workspace_id=original.name, attempt=0)
                runtime = sandbox if sandbox is not None else SandboxManager(settings)
                report["execution_attempted"] = True
                execution = ExecutionResult.model_validate(runtime.execute(workspace, build, 0))
                if safe_path(workspace, "metadata/cancel.requested").exists():
                    execution = execution.model_copy(update={"success": False, "cancelled": True, "error": "Manual execution cancelled"})
                evidence = {**execution.model_dump(mode="json"), "attempt": 0}
                manager.write(workspace, "metadata/execution.json", evidence)
                manager.write(workspace, "metadata/attempts/0/execution.json", evidence)
                if execution.success:
                    manager.status(workspace, "VALIDATING")
                    validation = validate_and_register(workspace, build, execution, settings, runtime)
                else:
                    validation = {"passed": False, "checks": [{"name": "Container execution", "kind": "execution",
                                  "target": build.entrypoint, "passed": False, "evidence": evidence}], "artifacts": [],
                                  "errors": [execution.error or "Container execution failed; outputs were not accepted"]}
                manager.write(workspace, "metadata/validation.json", validation)
                manager.write(workspace, "metadata/attempts/0/validation.json", validation)
                cancelled = execution.cancelled or safe_path(workspace, "metadata/cancel.requested").exists()
                if cancelled:
                    validation["passed"] = False
                    validation["errors"].append("Manual execution cancelled")
                    manager.write(workspace, "metadata/validation.json", validation)
                    manager.write(workspace, "metadata/attempts/0/validation.json", validation)
                status = "CANCELLED" if cancelled else "COMPLETED" if validation["passed"] else "FAILED"
                error = None if validation["passed"] else "; ".join(validation["errors"])[:4000]
                manifest = manager.read(workspace, "metadata/manifest.json", {})
                manifest.update(execution=evidence, validation_passed=validation["passed"],
                                selected_proposal_id=context["selected_plan"]["proposal"]["proposal_id"])
                manager.write(workspace, "metadata/manifest.json", manifest)
                manager.status(workspace, status, error=error)
                manager.event(workspace, status, "Manual debugging attempt finished", error=error, attempt=0)
                report.update(status=status, execution=evidence, validation=validation, error=error)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:4000]
                manager.status(workspace, "FAILED", error=error)
                manager.event(workspace, "FAILED", "Manual debugging failed; snapshot and available evidence retained", error=error)
                manager.write(workspace, "metadata/debug_error.json", {"error": error})
                report.update(status="FAILED", error=error)
            finally:
                job = manager.read(workspace, "metadata/job.json", {})
                manager.write(workspace, "metadata/job.json", {**job, "state": "finished", "finished_at": now(),
                              "exit_code": 0 if report.get("status") == "COMPLETED" else 1})
    return report
