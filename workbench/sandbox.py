"""Disposable Docker execution. No generated Python is ever imported on the host."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from filelock import FileLock

from .schemas import BuildResult, ExecutionResult
from .workspace import atomic_json, read_json, safe_path

AVAILABLE_DEPENDENCIES = frozenset({
    "duckdb", "pandas", "numpy", "pyarrow", "scipy", "scikit-learn", "networkx",
    "statsmodels", "matplotlib", "plotly", "joblib", "nbformat", "nbclient", "ipykernel",
})
MAX_LOG_BYTES = 2 * 1024 * 1024


def sandbox_identity() -> str:
    # Linux bind mounts retain numeric ownership. Match the unprivileged host
    # service so later inspection/retries can update container-created files.
    # Root launchers and Docker Desktop retain the isolated fallback UID.
    if sys.platform == "linux" and os.getuid() != 0 and os.getgid() != 0:
        return f"{os.getuid()}:{os.getgid()}"
    return "10001:10001"
REPORT_MARKER = "WORKBENCH_INSPECTION_JSON="


def _raw_digest(database: Path, row_limits=None, record_rows=False) -> str:
    """Hardened read-only fingerprint of original tables, with external access disabled."""
    import duckdb
    digest = hashlib.sha256()
    with duckdb.connect(str(database), read_only=True, config={
        "enable_external_access": False,
        "autoload_known_extensions": False,
        "autoinstall_known_extensions": False,
        "memory_limit": "512MB",
        "threads": 1,
    }) as connection:
        tables = connection.execute("SELECT table_name, table_type FROM information_schema.tables "
                                    "WHERE table_schema = 'raw' ORDER BY table_name").fetchall()
        if not tables:
            raise ValueError("Original raw tables are missing")
        digest.update(repr(tables).encode())
        for name, kind in tables:
            if kind != "BASE TABLE":
                raise ValueError("Original raw data must be a base table")
            quoted = '"' + name.replace('"', '""') + '"'
            row_count = connection.execute(f"SELECT count(*) FROM raw.{quoted}").fetchone()[0]
            if row_limits is not None:
                if record_rows:
                    row_limits[name] = row_count
                elif name not in row_limits or row_count != row_limits[name]:
                    raise ValueError("Original raw table row count changed")
            columns = connection.execute("SELECT column_name, data_type FROM information_schema.columns "
                                         "WHERE table_schema='raw' AND table_name=? ORDER BY ordinal_position",
                                         [name]).fetchall()
            digest.update(repr(columns).encode())
            # Commutative row digest permits harmless physical reordering while retaining duplicates.
            cursor = connection.execute(f"SELECT * FROM raw.{quoted}")
            count, total = 0, 0
            while rows := cursor.fetchmany(4096):
                for row in rows:
                    total = (total + int.from_bytes(hashlib.sha256(repr(row).encode()).digest(), "big")) % (1 << 256)
                    count += 1
            digest.update(f"{count}:{total}".encode())
    return digest.hexdigest()


def _copy_stream(stream, destination: Path):
    """Drain even after the limit to prevent pipe deadlock or unbounded disk growth."""
    remaining = MAX_LOG_BYTES
    truncated = False
    with destination.open("wb") as output:
        while chunk := stream.read(8192):
            if remaining:
                part = chunk[:remaining]
                output.write(part)
                output.flush()
                remaining -= len(part)
            if len(chunk) > remaining and remaining == 0:
                truncated = True
        if truncated:
            output.write(b"\n[workbench: log truncated at 2 MiB]\n")
    stream.close()


class SandboxManager:
    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def _docker(arguments, timeout=20):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.run(["docker", *arguments], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags)

    def health(self) -> dict:
        try:
            daemon = self._docker(["info", "--format", "{{.ServerVersion}}"], timeout=8)
            # Some Docker proxies return exit code zero alongside a backend
            # connection error and no version. A CLI exit is not readiness.
            version = daemon.stdout.strip()
            if daemon.returncode or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?(?:[-+~][\w.-]+)?", version):
                return {"available": False, "image_available": False,
                        "error": daemon.stderr.strip()[-1500:] or "Docker daemon did not return a valid server version"}
            image = self._docker(["image", "inspect", self.settings.sandbox_image, "--format", "{{.Id}}"], timeout=8)
            if image.returncode == 0 and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", image.stdout.strip()):
                return {"available": True, "image_available": True, "error": None}
            error = image.stderr.strip()[-1500:] or "Docker did not return a valid sandbox image ID"
            missing = any(message in error.lower() for message in ("no such image", "no such object"))
            if missing:
                error += "\nSandbox image is missing. Run the documented docker build command."
            return {"available": missing, "image_available": False, "error": error}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"available": False, "image_available": False, "error": str(exc)}

    def _confirm_cleanup(self, name: str) -> bool:
        """A CLI exit or unavailable daemon does not prove the container has stopped."""
        try:
            removed = self._docker(["rm", "--force", name], timeout=10)
            if removed.returncode == 0:
                return True
            inspect = self._docker(["inspect", name], timeout=8)
            return inspect.returncode != 0 and any(message in inspect.stderr.lower()
                    for message in ("no such object", "no such container"))
        except (OSError, subprocess.SubprocessError):
            return False

    def _recover_cleanup(self, workspace: Path) -> None:
        state_path = safe_path(workspace, "metadata/sandbox.json")
        prior = read_json(state_path, {}) or {}
        name = prior.get("container_name")
        if not name:
            return
        if not re.fullmatch(r"adw-[a-z0-9-]+", name) or not self._confirm_cleanup(name):
            prior.update(cleanup_failed=True)
            atomic_json(state_path, prior)
            raise RuntimeError(f"Cannot confirm removal of previous sandbox {name}; execution is blocked until Docker cleanup succeeds")
        atomic_json(state_path, {"container_name": None, "cleanup_failed": False, "finished_at": time.time()})

    def _command(self, workspace: Path, contract_directory: Path, name: str, mode: str) -> list[str]:
        # Explicit allowlist: never bind the workspace root, logs, metadata, host home, or Docker socket.
        memory = re.fullmatch(r"(\d+)([kmgt]?)(?:b)?", str(self.settings.sandbox_memory).lower())
        if not memory:
            raise ValueError("Sandbox memory must be a byte count or integer k/m/g/t size")
        memory_bytes = int(memory[1]) * 1024 ** ("kmgt".index(memory[2]) + 1 if memory[2] else 0)
        cmd = ["docker", "run", "--name", name, "--network", "none", "--user", sandbox_identity(),
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--read-only",
               "--pids-limit", "128", "--cpus", str(self.settings.sandbox_cpus),
               "--memory", self.settings.sandbox_memory, "--memory-swap", self.settings.sandbox_memory,
               "--memory-swappiness", "0", "--env", f"SANDBOX_MEMORY_BYTES={memory_bytes}",
               "--ulimit", "nofile=256:256", "--ulimit", "fsize=1073741824:1073741824",
               "--log-driver", "none", "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m,uid=10001,gid=10001",
               "--workdir", "/workspace", "--env", "HOME=/tmp", "--env", "MPLCONFIGDIR=/tmp/matplotlib",
               "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONUNBUFFERED=1",
               "--env", "OPENBLAS_NUM_THREADS=1", "--env", "OMP_NUM_THREADS=1"]
        # Docker CLI proxy defaults may contain credentials; explicitly suppress injection.
        for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            cmd += ["--env", variable + "="]
        for directory in ("input", "generated", "database", "results", "models", "notebooks"):
            source = safe_path(workspace, directory)
            source.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                # Local single-user sandbox: bind directories must allow the unprivileged container UID.
                source.chmod(source.stat().st_mode | 0o777)
                for child in source.iterdir():
                    if child.is_file() and not child.is_symlink():
                        child.chmod(child.stat().st_mode | (0o444 if directory in {"input", "generated"} else 0o666))
            readonly = directory in {"input", "generated"} or mode == "inspect"
            mount = f"type=bind,source={source.resolve()},target=/workspace/{directory}" + (",readonly" if readonly else "")
            if "," in str(source):
                raise ValueError("Docker mount paths cannot contain commas")
            cmd += ["--mount", mount]
        if "," in str(contract_directory):
            raise ValueError("Docker contract path cannot contain commas")
        cmd += ["--mount", f"type=bind,source={contract_directory.resolve()},target=/contract,readonly",
                self.settings.sandbox_image, "python", "-I", "/opt/workbench/runner.py", mode,
                "/contract/contract.json"]
        return cmd

    def _run(self, workspace: Path, contract: dict, mode: str, label: str) -> ExecutionResult:
        self._recover_cleanup(workspace)
        started = time.monotonic()
        stdout_rel, stderr_rel = f"logs/{label}.stdout.log", f"logs/{label}.stderr.log"
        stdout, stderr = safe_path(workspace, stdout_rel), safe_path(workspace, stderr_rel)
        stdout.parent.mkdir(parents=True, exist_ok=True)
        stdout.write_text("", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        requested_cancel = safe_path(workspace, "metadata/cancel.requested")
        if requested_cancel.exists():
            return ExecutionResult(success=False, exit_code=None, execution_time_seconds=time.monotonic()-started,
                                   stdout_path=stdout_rel, stderr_path=stderr_rel, cancelled=True, error="Execution cancelled")
        health = self.health()
        if not health["available"] or not health["image_available"]:
            stderr.write_text(health["error"] or "Docker unavailable", encoding="utf-8")
            return ExecutionResult(success=False, exit_code=None, execution_time_seconds=time.monotonic()-started,
                                   stdout_path=stdout_rel, stderr_path=stderr_rel, error=health["error"])
        name = "adw-" + re.sub(r"[^a-z0-9-]", "", workspace.name.lower())[:32] + "-" + uuid.uuid4().hex[:10]
        state_path = safe_path(workspace, "metadata/sandbox.json")
        cancel_path = safe_path(workspace, "metadata/sandbox_cancel.json")
        process = None
        timed_out = cancelled = oom = False
        exit_code = None
        error = None
        readers = []
        try:
            with tempfile.TemporaryDirectory(prefix="adw-contract-") as temporary:
                contract_directory = Path(temporary)
                if os.name != "nt":
                    contract_directory.chmod(0o755)
                contract_file = contract_directory / "contract.json"
                contract_file.write_text(json.dumps(contract), encoding="utf-8")
                if os.name != "nt":
                    contract_file.chmod(0o444)
                command = self._command(workspace, contract_directory, name, mode)
                atomic_json(state_path, {"container_name": name, "mode": mode, "started_at": time.time()})
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
                for stream, destination in ((process.stdout, stdout), (process.stderr, stderr)):
                    thread = threading.Thread(target=_copy_stream, args=(stream, destination), daemon=True)
                    thread.start()
                    readers.append(thread)
                deadline = time.monotonic() + self.settings.sandbox_timeout_seconds
                while process.poll() is None:
                    cancel = read_json(cancel_path, {}) or {}
                    if cancel.get("container_name") == name or requested_cancel.exists():
                        cancelled = True
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    time.sleep(0.1)
                if timed_out or cancelled:
                    try:
                        self._docker(["kill", name], timeout=10)
                    except (OSError, subprocess.SubprocessError):
                        pass
                try:
                    exit_code = process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.kill()
                    exit_code = process.wait(timeout=5)
                try:
                    info = self._docker(["inspect", "--format", "{{json .State}}", name], timeout=8)
                    if info.returncode == 0:
                        state = json.loads(info.stdout)
                        oom = bool(state.get("OOMKilled"))
                        exit_code = state.get("ExitCode", exit_code)
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
                cancelled = cancelled or requested_cancel.exists() or (read_json(cancel_path, {}) or {}).get("container_name") == name
                if timed_out:
                    error = f"Execution exceeded {self.settings.sandbox_timeout_seconds} seconds"
                elif cancelled:
                    error = "Execution cancelled"
                elif oom:
                    error = "Container exceeded its memory limit"
                elif exit_code:
                    error = f"Container exited with code {exit_code}"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = str(exc)
            with stderr.open("a", encoding="utf-8") as handle:
                handle.write(error + "\n")
        finally:
            cleaned = self._confirm_cleanup(name)
            if not cleaned:
                error = (error + "; " if error else "") + f"Cannot confirm container cleanup: {name}. Further execution is blocked until cleanup succeeds."
            if process and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for thread in readers:
                thread.join(timeout=5)
            atomic_json(state_path, {"container_name": None if cleaned else name, "mode": mode,
                                    "cleanup_failed": not cleaned, "finished_at": time.time()})
        return ExecutionResult(success=exit_code == 0 and not any((timed_out, cancelled, oom, error)),
                               exit_code=exit_code, execution_time_seconds=time.monotonic()-started,
                               stdout_path=stdout_rel, stderr_path=stderr_rel, timed_out=timed_out,
                               cancelled=cancelled, oom_killed=oom, error=error)

    def execute(self, workspace: Path, build: BuildResult, attempt: int) -> ExecutionResult:
        if build.entrypoint != "generated/solution.py":
            raise ValueError("Only the approved generated/solution.py entry point may execute")
        safe_path(workspace, build.entrypoint)
        database = safe_path(workspace, "database/workspace.duckdb")
        with FileLock(str(safe_path(workspace, "metadata/database.lock")), timeout=self.settings.sandbox_timeout_seconds + 30):
            self._recover_cleanup(workspace)
            self._fresh_attempt(workspace, attempt)
            with tempfile.TemporaryDirectory(prefix="adw-original-") as temporary:
                backup = Path(temporary) / "workspace.duckdb"
                original_rows = {}
                before = _raw_digest(database, row_limits=original_rows, record_rows=True)
                shutil.copy2(database, backup)
                wal = database.with_suffix(database.suffix + ".wal")
                if wal.exists():
                    shutil.copy2(wal, backup.with_suffix(backup.suffix + ".wal"))
                result = self._run(workspace, {"entrypoint": build.entrypoint,
                                   "required_dependencies": build.required_dependencies}, "execute", f"attempt-{attempt}")
                if (read_json(safe_path(workspace, "metadata/sandbox.json"), {}) or {}).get("cleanup_failed"):
                    # Do not race a possibly-live container by reading/restoring its database.
                    shutil.copy2(backup, safe_path(workspace, "metadata/cleanup_restore.duckdb"))
                    return result
                try:
                    if database.is_symlink() or _raw_digest(database, row_limits=original_rows) != before:
                        raise ValueError("Sandbox modified the immutable raw tables")
                except Exception as exc:
                    # Host-only backup is never mounted into generated-code containers.
                    if database.exists() or database.is_symlink():
                        database.unlink()
                    wal.unlink(missing_ok=True)
                    shutil.copy2(backup, database)
                    saved_wal = backup.with_suffix(backup.suffix + ".wal")
                    if saved_wal.exists():
                        shutil.copy2(saved_wal, wal)
                    result = result.model_copy(update={"success": False, "error": f"Original database restored: {exc}"})
                return result

    @staticmethod
    def _fresh_attempt(workspace: Path, attempt: int) -> None:
        """Archive prior evidence, then prevent old outputs satisfying a new attempt."""
        database = safe_path(workspace, "database/workspace.duckdb")
        baseline = safe_path(workspace, "metadata/ingested.duckdb")
        if not baseline.exists():
            shutil.copy2(database, baseline)
        archive = safe_path(workspace, f"metadata/prior_outputs/{attempt}-{uuid.uuid4().hex[:10]}")
        archive.mkdir(parents=True)
        shutil.copy2(database, archive / "workspace.duckdb")
        for directory in ("results", "models", "notebooks"):
            folder = safe_path(workspace, directory)
            folder.mkdir(exist_ok=True)
            for item in list(folder.iterdir()):
                # Validate every concrete source before any move; reject symlinks/junctions.
                source = safe_path(workspace, item.relative_to(workspace).as_posix())
                target = safe_path(workspace, (archive / directory / item.name).relative_to(workspace).as_posix())
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
        wal = database.with_suffix(database.suffix + ".wal")
        if wal.exists():
            shutil.move(str(wal), str(archive / "workspace.duckdb.wal"))
        shutil.copy2(baseline, database)

    def inspect(self, workspace: Path, checks: list[dict]) -> dict:
        with FileLock(str(safe_path(workspace, "metadata/database.lock")), timeout=self.settings.sandbox_timeout_seconds + 30):
            result = self._run(workspace, {"checks": checks}, "inspect", "validation-" + uuid.uuid4().hex[:8])
        if not result.success:
            return {"checks": [], "artifacts": [], "error": result.error or "Sandbox inspection failed"}
        output = safe_path(workspace, result.stdout_path).read_text(encoding="utf-8", errors="replace")
        try:
            payload = json.loads(output.rsplit(REPORT_MARKER, 1)[1].strip())
            if not isinstance(payload, dict) or not isinstance(payload.get("checks"), list):
                raise ValueError("Malformed inspection result")
            return payload
        except (ValueError, IndexError) as exc:
            return {"checks": [], "artifacts": [], "error": f"Sandbox returned no valid inspection evidence: {exc}"}

    def cancel(self, workspace: Path) -> None:
        safe_path(workspace, "metadata/cancel.requested").touch()
        active = read_json(safe_path(workspace, "metadata/sandbox.json"), {}) or {}
        name = active.get("container_name")
        if name and re.fullmatch(r"adw-[a-z0-9-]+", name):
            atomic_json(safe_path(workspace, "metadata/sandbox_cancel.json"), {"container_name": name})
            try:
                self._docker(["kill", name], timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
