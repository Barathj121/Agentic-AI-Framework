import json
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from workbench.applications import BUILTIN, OWNER_LABEL, ApplicationRegistry
from workbench.settings import Settings

IMAGE_ID = "sha256:" + "a" * 64
CONTAINER_ID = "b" * 64


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.info = None
        self.image = {"Id": IMAGE_ID, "Os": "linux", "Config": {}}
        self.fail = None
        self.timeout_create = False

    def __call__(self, args, timeout=20):
        self.calls.append(args)
        if self.fail and args[0] == self.fail:
            return subprocess.CompletedProcess(args, 1, "", "Cannot connect to Docker daemon")
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, json.dumps([self.image]), "")
        if args[0] == "create":
            labels = dict(args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "--label")
            self.info = {"Id": CONTAINER_ID, "Image": args[-1], "Config": {"Labels": labels},
                         "State": {"Status": "created", "Running": False},
                         "NetworkSettings": {"Ports": {"8501/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49152"}]}}}
            if self.timeout_create:
                self.timeout_create = False
                raise subprocess.TimeoutExpired("docker create", timeout)
            return subprocess.CompletedProcess(args, 0, CONTAINER_ID, "")
        if args[:2] == ["container", "inspect"]:
            if self.info is None:
                return subprocess.CompletedProcess(args, 1, "", "Error: No such container")
            return subprocess.CompletedProcess(args, 0, json.dumps([self.info]), "")
        if args[0] == "start":
            self.info["State"] = {"Status": "running", "Running": True}
        elif args[0] == "stop":
            self.info["State"] = {"Status": "exited", "Running": False}
        elif args[0] == "rm":
            self.info = None
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, CONTAINER_ID, "")


@pytest.fixture
def registry(tmp_path, monkeypatch):
    manager = ApplicationRegistry(Settings(workspace_root=tmp_path / "workspaces"))
    fake = FakeDocker()
    monkeypatch.setattr(manager, "_docker", fake)
    return manager, fake


def register(manager):
    return manager.register("Customer Insights", "customer-insights:latest", 8501, "Example", trusted=True)


def test_builtin_always_registered_without_docker(registry):
    manager, fake = registry
    assert manager.list() == [BUILTIN]
    assert fake.calls == []
    with pytest.raises(ValueError, match="inside"):
        manager.start("migration")


def test_register_pins_local_image_and_persists(registry):
    manager, fake = registry
    app = register(manager)
    assert app["image_id"] == IMAGE_ID
    assert manager.list()[1] == app
    assert ApplicationRegistry(manager.settings).list()[1] == app
    assert fake.calls == [["image", "inspect", "customer-insights:latest"]]
    with pytest.raises(ValueError, match="already registered"):
        register(manager)


@pytest.mark.parametrize("name,image,port,trusted", [
    ("Name", "image:latest", 8501, False), ("", "image:latest", 8501, True),
    ("Name", "--help", 8501, True), ("Name", "bad image", 8501, True),
    ("Name", "image:latest", 0, True), ("Name", "image:latest", 65536, True),
    ("Name", "image:latest", True, True),
])
def test_invalid_registration_never_calls_docker(registry, name, image, port, trusted):
    manager, fake = registry
    with pytest.raises(ValueError):
        manager.register(name, image, port, trusted=trusted)
    assert not fake.calls


def test_missing_local_image_not_registered_or_pulled(registry):
    manager, fake = registry
    fake.fail = "image"
    with pytest.raises(RuntimeError):
        register(manager)
    assert manager.list() == [BUILTIN]
    assert all("pull" not in call for call in fake.calls)


def test_image_declared_volumes_rejected(registry):
    manager, fake = registry
    fake.image["Config"]["Volumes"] = {"/data": {}}
    with pytest.raises(ValueError, match="volumes"):
        register(manager)


def test_start_status_stop_and_restart(registry):
    manager, fake = registry
    app = register(manager)
    running = manager.start(app["id"])
    assert running["status"] == "running"
    assert running["url"] == "http://127.0.0.1:49152/"
    assert manager.status(app["id"])["container_id"] == CONTAINER_ID
    manager.start(app["id"])
    assert sum(call[0] == "create" for call in fake.calls) == 1
    stopped = manager.stop(app["id"])
    assert stopped["status"] == "stopped" and stopped["url"] is None
    assert manager.list()[1]["container_id"] is None
    assert fake.calls[-1] == ["rm", CONTAINER_ID]
    assert manager.start(app["id"])["status"] == "running"


def test_create_enforces_local_image_isolation_and_limits(registry):
    manager, fake = registry
    app = register(manager)
    fake.image["Id"] = "sha256:" + "c" * 64  # Retagging never changes the approved image.
    manager.start(app["id"])
    command = next(call for call in fake.calls if call[0] == "create")
    assert command[-1] == IMAGE_ID
    for key, value in (("--pull", "never"), ("--publish", "127.0.0.1::8501"),
                       ("--user", "10001:10001"), ("--cap-drop", "ALL"),
                       ("--cpus", "2"), ("--memory", "2g"), ("--pids-limit", "128")):
        assert command[command.index(key) + 1] == value
    assert "--read-only" in command and "no-new-privileges:true" in command
    assert not {"--mount", "--volume", "--privileged", "--env-file"}.intersection(command)
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert variable + "=" in command


@pytest.mark.parametrize("operation", ["start", "stop", "status"])
def test_wrong_owner_prevents_lifecycle_actions(registry, operation):
    manager, fake = registry
    app = register(manager)
    manager.start(app["id"])
    fake.info["Config"]["Labels"][OWNER_LABEL] = "someone-else"
    before = len(fake.calls)
    with pytest.raises(ValueError, match="ownership"):
        getattr(manager, operation)(app["id"])
    assert all(call[0] == "container" for call in fake.calls[before:])


def test_wrong_image_prevents_stop(registry):
    manager, fake = registry
    app = register(manager)
    manager.start(app["id"])
    fake.info["Image"] = "sha256:" + "c" * 64
    with pytest.raises(ValueError, match="does not match"):
        manager.stop(app["id"])


def test_nonlocal_binding_never_returns_url(registry):
    manager, fake = registry
    app = register(manager)
    manager.start(app["id"])
    fake.info["NetworkSettings"]["Ports"]["8501/tcp"][0]["HostIp"] = "0.0.0.0"
    with pytest.raises(ValueError, match="localhost"):
        manager.status(app["id"])
    manager.stop(app["id"])  # Ownership verification still allows stopping an unsafe binding.


def test_docker_unavailable_does_not_claim_stopped(registry):
    manager, fake = registry
    app = register(manager)
    manager.start(app["id"])
    fake.fail = "container"
    with pytest.raises(RuntimeError, match="Cannot connect"):
        manager.stop(app["id"])
    assert manager.list()[1]["container_id"] == CONTAINER_ID


def test_stop_failure_preserves_container_identity(registry):
    manager, fake = registry
    app = register(manager)
    manager.start(app["id"])
    fake.fail = "stop"
    with pytest.raises(RuntimeError):
        manager.stop(app["id"])
    assert manager.list()[1]["container_id"] == CONTAINER_ID
    assert not any(call[0] == "rm" for call in fake.calls)


def test_interrupted_create_recovers_owned_container_by_name(registry):
    manager, fake = registry
    app = register(manager)
    fake.timeout_create = True
    with pytest.raises(RuntimeError, match="timed out"):
        manager.start(app["id"])
    record = manager.list()[1]
    assert record["container_name"] and record["container_id"] is None
    assert manager.start(app["id"])["status"] == "running"
    assert sum(call[0] == "create" for call in fake.calls) == 1


def test_concurrent_starts_create_one_container(registry):
    manager, fake = registry
    app = register(manager)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: manager.start(app["id"]), range(2)))
    assert all(result["status"] == "running" for result in results)
    assert sum(call[0] == "create" for call in fake.calls) == 1


def test_malformed_registry_is_not_replaced(registry):
    manager, fake = registry
    manager.path.write_text('{"version": 999}', encoding="utf-8")
    with pytest.raises(ValueError, match="format"):
        register(manager)
    assert manager.path.read_text() == '{"version": 999}'
    assert not fake.calls
