import dataclasses
import io
import json
import subprocess
import concurrent.futures
import time
from pathlib import Path

import duckdb
import pytest

from workbench.data import ingest_csv
from workbench.sandbox import sandbox_identity, MAX_LOG_BYTES, SandboxManager, _copy_stream, _raw_digest
from workbench.schemas import BuildResult, ExecutionResult, GeneratedFile, OutputSpec
from workbench.settings import Settings
from workbench.workspace import WorkspaceManager


@pytest.fixture
def sandbox_workspace(tmp_path):
    settings = Settings(workspace_root=tmp_path / "workspaces", sandbox_timeout_seconds=20)
    workspace = WorkspaceManager(settings.workspace_root).create("sandbox test", "Count records")
    ingest_csv(workspace, b"id,value\n1,10\n2,20\n", "data.csv", settings)
    return workspace, settings


def build_code(workspace, source, dependencies=None):
    (workspace / "generated/solution.py").write_text(source, encoding="utf-8")
    return BuildResult(files=[GeneratedFile(path="generated/solution.py", content=source)],
                       required_dependencies=dependencies or [],
                       expected_outputs=[OutputSpec(name="result", type="json", location="results/result.json")])


def test_docker_command_restricts_mounts_and_privileges(sandbox_workspace, tmp_path):
    workspace, settings = sandbox_workspace
    command = SandboxManager(settings)._command(workspace, tmp_path / "contract", "adw-test", "execute")
    assert command[:2] == ["docker", "run"]
    for option, value in (("--network", "none"), ("--user", sandbox_identity()), ("--cap-drop", "ALL"),
                          ("--security-opt", "no-new-privileges:true"), ("--pids-limit", "128")):
        assert command[command.index(option) + 1] == value
    assert "--read-only" in command and "--privileged" not in command
    mounts = [command[i + 1] for i, token in enumerate(command) if token == "--mount"]
    assert len(mounts) == 7
    assert all("metadata" not in mount and "logs" not in mount and "docker.sock" not in mount for mount in mounts)
    assert next(m for m in mounts if "target=/workspace/input" in m).endswith(",readonly")
    assert next(m for m in mounts if "target=/workspace/generated" in m).endswith(",readonly")


def test_inspection_has_no_writable_workspace_mounts(sandbox_workspace, tmp_path):
    workspace, settings = sandbox_workspace
    command = SandboxManager(settings)._command(workspace, tmp_path, "adw-test", "inspect")
    mounts = [command[i + 1] for i, token in enumerate(command) if token == "--mount"]
    assert all(mount.endswith(",readonly") for mount in mounts)


def test_missing_docker_fails_without_running_code(sandbox_workspace, monkeypatch):
    workspace, settings = sandbox_workspace
    manager = SandboxManager(settings)
    monkeypatch.setattr(manager, "health", lambda: {"available": False, "image_available": False, "error": "daemon unavailable"})
    build = build_code(workspace, "raise RuntimeError('never run on host')")
    result = manager.execute(workspace, build, 1)
    assert not result.success and result.exit_code is None
    assert "daemon unavailable" in result.error
    assert "daemon unavailable" in (workspace / result.stderr_path).read_text()


@pytest.mark.parametrize('version', ['', '\n', '<no value>', 'not-a-version'])
def test_health_rejects_zero_exit_backend_failure(sandbox_workspace, monkeypatch, version):
    _, settings = sandbox_workspace
    manager = SandboxManager(settings)
    calls = []
    error = 'Error response from daemon: failed to connect to the backend: timed out dialing Hyper-V socket'

    def docker(arguments, **kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, version, error)

    monkeypatch.setattr(manager, '_docker', docker)
    assert manager.health() == {'available': False, 'image_available': False, 'error': error}
    assert len(calls) == 1


@pytest.mark.parametrize('error,missing', [
    ('Error response from daemon: failed to connect to the backend: timed out dialing Hyper-V socket', False),
    ('Error response from daemon: No such image: agentic-workbench-sandbox:latest', True),
])
def test_health_preserves_image_inspection_error(sandbox_workspace, monkeypatch, error, missing):
    _, settings = sandbox_workspace
    manager = SandboxManager(settings)
    results = iter([subprocess.CompletedProcess([], 0, '29.5.3\n', ''),
                    subprocess.CompletedProcess([], 1, '', error)])
    monkeypatch.setattr(manager, '_docker', lambda *args, **kwargs: next(results))
    health = manager.health()
    assert health['available'] is missing and not health['image_available']
    assert error in health['error']
    assert ('docker build' in health['error']) is missing


def test_health_confirms_version_and_image_evidence(sandbox_workspace, monkeypatch):
    _, settings = sandbox_workspace
    manager = SandboxManager(settings)
    results = iter([subprocess.CompletedProcess([], 0, '29.5.3\n', 'WARNING: advisory\n'),
                    subprocess.CompletedProcess([], 0, 'sha256:' + 'a' * 64 + '\n', '')])
    monkeypatch.setattr(manager, '_docker', lambda *args, **kwargs: next(results))
    assert manager.health() == {'available': True, 'image_available': True, 'error': None}


def test_bounded_log_capture_drains_stream(tmp_path):
    stream = io.BytesIO(b"a" * (MAX_LOG_BYTES + 50000))
    path = tmp_path / "log"
    _copy_stream(stream, path)
    assert stream.closed
    assert path.stat().st_size < MAX_LOG_BYTES + 100
    assert b"truncated" in path.read_bytes()[-100:]


def test_raw_modification_is_restored(sandbox_workspace, monkeypatch):
    workspace, settings = sandbox_workspace
    database = workspace / "database/workspace.duckdb"
    before = _raw_digest(database)
    manager = SandboxManager(settings)

    def simulate_corruption(*args):
        with duckdb.connect(str(database)) as con:
            con.execute("DELETE FROM raw.source")
        return ExecutionResult(success=True, exit_code=0, execution_time_seconds=0,
                               stdout_path="logs/out.log", stderr_path="logs/err.log")

    monkeypatch.setattr(manager, "_run", simulate_corruption)
    result = manager.execute(workspace, build_code(workspace, "pass"), 1)
    assert not result.success
    assert "restored" in result.error
    assert _raw_digest(database) == before


def test_cancel_only_stops_recorded_workbench_container(sandbox_workspace, monkeypatch):
    workspace, settings = sandbox_workspace
    (workspace / "metadata/sandbox.json").write_text(json.dumps({"container_name": "adw-test-123"}))
    calls = []
    manager = SandboxManager(settings)
    monkeypatch.setattr(manager, "_docker", lambda args, **kwargs: calls.append(args))
    manager.cancel(workspace)
    assert calls == [["kill", "adw-test-123"]]
    assert json.loads((workspace / "metadata/sandbox_cancel.json").read_text())["container_name"] == "adw-test-123"


def test_no_unverified_inspection_success(sandbox_workspace, monkeypatch):
    workspace, settings = sandbox_workspace
    manager = SandboxManager(settings)
    (workspace / "logs/check.log").write_text("some text without evidence")
    monkeypatch.setattr(manager, "_run", lambda *args: ExecutionResult(success=True, exit_code=0,
                        execution_time_seconds=0, stdout_path="logs/check.log", stderr_path="logs/err.log"))
    report = manager.inspect(workspace, [])
    assert report["error"] and not report["checks"]


def test_unconfirmed_cleanup_preserves_name_and_blocks_retry(sandbox_workspace, monkeypatch):
    workspace, settings = sandbox_workspace
    manager = SandboxManager(settings)
    (workspace / "metadata/sandbox.json").write_text(json.dumps({"container_name": "adw-orphan-123", "cleanup_failed": True}))
    monkeypatch.setattr(manager, "_docker", lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "Cannot connect to the Docker daemon"))
    assert not manager._confirm_cleanup("adw-orphan-123")
    with pytest.raises(RuntimeError, match="execution is blocked"):
        manager.execute(workspace, build_code(workspace, "pass"), 2)
    assert json.loads((workspace / "metadata/sandbox.json").read_text())["container_name"] == "adw-orphan-123"


def test_cleanup_confirmed_absent_allows_recovery(sandbox_workspace, monkeypatch):
    workspace, settings = sandbox_workspace
    manager = SandboxManager(settings)
    (workspace / "metadata/sandbox.json").write_text(json.dumps({"container_name": "adw-orphan-123", "cleanup_failed": True}))
    monkeypatch.setattr(manager, "_docker", lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "Error: No such object: adw-orphan-123"))
    manager._recover_cleanup(workspace)
    assert json.loads((workspace / "metadata/sandbox.json").read_text())["container_name"] is None


def _live_manager(settings):
    manager = SandboxManager(settings)
    health = manager.health()
    assert health["available"] and health["image_available"], health
    return manager


@pytest.mark.docker
def test_docker_success_failure_and_cleanup(sandbox_workspace):
    workspace, settings = sandbox_workspace
    manager = _live_manager(settings)
    result = manager.execute(workspace, build_code(workspace, "from pathlib import Path\nPath('results/result.json').write_text('{\"count\":2}')\nprint('computed')"), 1)
    assert result.success, result
    assert "computed" in (workspace / result.stdout_path).read_text()
    assert json.loads((workspace / "results/result.json").read_text()) == {"count": 2}
    failed = manager.execute(workspace, build_code(workspace, "raise ValueError('intentional failure')"), 2)
    assert not failed.success and failed.exit_code != 0
    assert "intentional failure" in (workspace / failed.stderr_path).read_text()
    remaining = manager._docker(["ps", "-aq", "--filter", f"name=adw-{workspace.name.lower().replace('_', '')}"])
    assert not remaining.stdout.strip()


@pytest.mark.docker
def test_docker_timeout(sandbox_workspace):
    workspace, settings = sandbox_workspace
    settings = dataclasses.replace(settings, sandbox_timeout_seconds=2)
    result = _live_manager(settings).execute(workspace, build_code(workspace, "import time\ntime.sleep(60)"), 1)
    assert not result.success and result.timed_out


@pytest.mark.docker
def test_docker_cancellation_stops_running_attempt(sandbox_workspace):
    workspace, settings = sandbox_workspace
    manager = _live_manager(settings)
    build = build_code(workspace, "import time\ntime.sleep(60)")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(manager.execute, workspace, build, 1)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = workspace / "metadata/sandbox.json"
            if state.exists() and json.loads(state.read_text()).get("container_name"):
                break
            time.sleep(0.1)
        manager.cancel(workspace)
        result = future.result(timeout=30)
    assert not result.success and result.cancelled


@pytest.mark.docker
def test_docker_missing_dependency(sandbox_workspace):
    workspace, settings = sandbox_workspace
    result = _live_manager(settings).execute(workspace, build_code(workspace, "print('should not run')", ["workbench-package-does-not-exist"]), 1)
    assert not result.success
    assert "not installed" in (workspace / result.stderr_path).read_text()


@pytest.mark.docker
def test_docker_filesystem_network_and_originals(sandbox_workspace):
    workspace, settings = sandbox_workspace
    source = """from pathlib import Path
import os, socket
assert os.getuid() != 0
assert not Path('/workspace/metadata').exists()
assert not Path('/workspace/logs').exists()
assert not Path('/var/run/docker.sock').exists()
for name in ['/forbidden', 'input/source.csv', 'generated/forbidden.py']:
    try:
        Path(name).write_text('bad')
    except OSError:
        pass
    else:
        raise AssertionError('write unexpectedly allowed: ' + name)
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('network unexpectedly available')
print('isolation checks passed')
"""
    before = (workspace / "input/source.csv").read_bytes()
    result = _live_manager(settings).execute(workspace, build_code(workspace, source), 1)
    assert result.success, (workspace / result.stderr_path).read_text()
    assert (workspace / "input/source.csv").read_bytes() == before


@pytest.mark.docker
def test_docker_memory_limit(sandbox_workspace):
    workspace, settings = sandbox_workspace
    settings = dataclasses.replace(settings, sandbox_memory="128m")
    result = _live_manager(settings).execute(workspace, build_code(workspace, "x = bytearray(1024 * 1024 * 1024)\nfor i in range(0, len(x), 4096):\n    x[i] = 1\nprint(len(x))"), 1)
    assert not result.success
    assert result.oom_killed or result.exit_code in (137, -9) or "MemoryError" in (workspace / result.stderr_path).read_text()


@pytest.mark.docker
def test_docker_raw_changes_fail_and_restore(sandbox_workspace):
    workspace, settings = sandbox_workspace
    before = _raw_digest(workspace / "database/workspace.duckdb")
    source = "import duckdb\nwith duckdb.connect('database/workspace.duckdb') as con:\n    con.execute('DELETE FROM raw.source')"
    result = _live_manager(settings).execute(workspace, build_code(workspace, source), 1)
    assert not result.success and "restored" in result.error
    assert _raw_digest(workspace / "database/workspace.duckdb") == before


def test_linux_sandbox_preserves_nonroot_output_ownership(monkeypatch):
    import workbench.sandbox as module
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.os, "getuid", lambda: 999)
    monkeypatch.setattr(module.os, "getgid", lambda: 994)
    assert module.sandbox_identity() == "999:994"
    monkeypatch.setattr(module.os, "getuid", lambda: 0)
    assert module.sandbox_identity() == "10001:10001"
