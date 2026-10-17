"""Local application registry and explicitly requested Docker application lifecycle."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path

from filelock import FileLock

from .sandbox import SandboxManager
from .workspace import atomic_json, now, read_json, safe_path

BUILTIN = {"id": "migration", "name": "Migration Studio", "kind": "builtin",
           "description": "AI-assisted source-to-target data migration", "page": "migration"}
OWNER_LABEL = "org.agentic-workbench.registry"
APP_LABEL = "org.agentic-workbench.application"
_IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")


class ApplicationRegistry:
    """One registry per workspace root; Docker operations are serialized with persistence.

    Image names are resolved at registration, and only that exact local image ID is
    launched. The launcher never pulls an image, executes a shell, or mounts host files.
    """

    def __init__(self, settings):
        self.settings = settings
        root = Path(settings.workspace_root).resolve()
        self.directory = safe_path(root, ".applications")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = safe_path(self.directory, "registry.json")
        self.lock = FileLock(str(safe_path(self.directory, "registry.lock")), timeout=30)
        self.owner = hashlib.sha256(str(root).encode()).hexdigest()[:24]

    @staticmethod
    def _docker(arguments, timeout=20):
        return SandboxManager._docker(arguments, timeout=timeout)

    def _call(self, arguments, timeout=20):
        try:
            result = self._docker(arguments, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"Docker command unavailable or timed out: {type(exc).__name__}") from exc
        return result

    def _checked(self, arguments, timeout=20):
        result = self._call(arguments, timeout)
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[-1200:] or "Docker command failed")
        return result.stdout.strip()

    def _load(self):
        data = read_json(self.path, {"version": 1, "applications": []})
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("applications"), list):
            raise ValueError("Application registry format is invalid")
        for app in data["applications"]:
            if (not isinstance(app, dict) or not re.fullmatch(r"app_[a-f0-9]{24}", str(app.get("id", "")))
                    or app.get("kind") != "docker" or not _IMAGE_ID.fullmatch(str(app.get("image_id", "")))
                    or type(app.get("internal_port")) is not int or not 1 <= app["internal_port"] <= 65535):
                raise ValueError("Invalid application registry record")
        return data

    def _find(self, data, app_id):
        if app_id == BUILTIN["id"]:
            raise ValueError("Migration Studio opens inside the workbench; it has no external container")
        return next((app for app in data["applications"] if app["id"] == app_id), None) or self._missing()

    @staticmethod
    def _missing():
        raise ValueError("Application is not registered")

    def list(self):
        with self.lock:
            return [dict(BUILTIN), *self._load()["applications"]]

    def register(self, name: str, image: str, internal_port: int, description: str = "", *, trusted: bool = False):
        if not trusted:
            raise ValueError("Confirm that this is a trusted local image before registering it")
        name, image, description = str(name).strip(), str(image).strip(), str(description).strip()
        if not name or len(name) > 100 or any(ord(c) < 32 for c in name):
            raise ValueError("Application name must contain 1–100 printable characters")
        if (not image or len(image) > 255 or image.startswith("-")
                or any(c.isspace() or ord(c) < 32 for c in image)):
            raise ValueError("Supply a local Docker image name or ID")
        if type(internal_port) is not int or not 1 <= internal_port <= 65535:
            raise ValueError("Internal port must be an integer between 1 and 65535")
        if len(description) > 2000:
            raise ValueError("Description must be at most 2000 characters")
        with self.lock:
            data = self._load()
            if name.casefold() in {app["name"].casefold() for app in [BUILTIN, *data["applications"]]}:
                raise ValueError("An application with that name is already registered")
            details = json.loads(self._checked(["image", "inspect", image]))
            if not isinstance(details, list) or len(details) != 1 or not _IMAGE_ID.fullmatch(str(details[0].get("Id", ""))):
                raise ValueError("Docker did not return a valid local image ID")
            if details[0].get("Os", "linux") != "linux":
                raise ValueError("Only Linux container images are supported")
            if details[0].get("Config", {}).get("Volumes"):
                raise ValueError("Images declaring volumes are unsupported by this local, temporary launcher")
            app = {"id": "app_" + uuid.uuid4().hex[:24], "kind": "docker", "name": name,
                   "image": image, "image_id": details[0]["Id"], "internal_port": internal_port,
                   "description": description, "created_at": now(), "container_id": None,
                   "container_name": None, "status": "registered"}
            data["applications"].append(app)
            atomic_json(self.path, data)
            return dict(app)

    def _inspect_owned(self, app):
        reference = app.get("container_id") or app.get("container_name")
        if not reference:
            return None
        if not (re.fullmatch(r"[a-f0-9]{64}", str(reference)) or
                re.fullmatch(r"adw-app-[a-f0-9]{24}-[a-f0-9]{10}", str(reference))):
            raise ValueError("Invalid recorded container identity")
        result = self._call(["container", "inspect", reference], timeout=10)
        if result.returncode:
            if any(message in result.stderr.lower() for message in ("no such object", "no such container")):
                return None
            raise RuntimeError(result.stderr.strip()[-1200:] or "Cannot determine container status")
        records = json.loads(result.stdout)
        if not isinstance(records, list) or len(records) != 1:
            raise ValueError("Invalid Docker container inspection")
        info = records[0]
        labels = info.get("Config", {}).get("Labels", {}) or {}
        if labels.get(OWNER_LABEL) != self.owner or labels.get(APP_LABEL) != app["id"]:
            raise ValueError("Container ownership could not be verified; refusing lifecycle action")
        if info.get("Image") != app["image_id"] or not re.fullmatch(r"[a-f0-9]{64}", str(info.get("Id", ""))):
            raise ValueError("Recorded application image does not match the container")
        return info

    @staticmethod
    def _status(app, info):
        result = dict(app)
        result["url"] = None
        if info is None:
            result["status"] = "stopped" if app.get("container_name") else app.get("status", "registered")
            return result
        result["container_id"] = info["Id"]
        result["status"] = str(info.get("State", {}).get("Status", "unknown"))
        if info.get("State", {}).get("Running"):
            bindings = info.get("NetworkSettings", {}).get("Ports", {}).get(f"{app['internal_port']}/tcp") or []
            if len(bindings) != 1 or bindings[0].get("HostIp") != "127.0.0.1":
                raise ValueError("Application port is not bound exclusively to localhost")
            port = bindings[0].get("HostPort", "")
            if not str(port).isdigit() or not 1 <= int(port) <= 65535:
                raise ValueError("Invalid application host port")
            result["url"] = f"http://127.0.0.1:{int(port)}/"
        return result

    def status(self, app_id):
        with self.lock:
            app = self._find(self._load(), app_id)
            return self._status(app, self._inspect_owned(app))

    def _create_arguments(self, app):
        arguments = ["create", "--name", app["container_name"], "--pull", "never",
                     "--label", f"{OWNER_LABEL}={self.owner}", "--label", f"{APP_LABEL}={app['id']}",
                     "--publish", f"127.0.0.1::{app['internal_port']}",
                     "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                     "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=256m,uid=10001,gid=10001",
                     "--cpus", "2", "--memory", "2g", "--memory-swap", "2g", "--pids-limit", "128",
                     "--restart", "no", "--log-driver", "json-file", "--log-opt", "max-size=2m",
                     "--log-opt", "max-file=1", "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1"]
        # Suppress the Docker client's configured proxy injection, which can contain credentials.
        for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            arguments += ["--env", variable + "="]
        return [*arguments, app["image_id"]]

    def start(self, app_id):
        with self.lock:
            data = self._load()
            app = self._find(data, app_id)
            info = self._inspect_owned(app)
            if info and info.get("State", {}).get("Running"):
                return self._status(app, info)
            # Persist the intended name before Docker runs so interrupted starts are recoverable.
            if info is None:
                app.update(container_id=None, container_name=f"adw-app-{app['id'][4:]}-{uuid.uuid4().hex[:10]}", status="starting")
                atomic_json(self.path, data)
                container_id = self._checked(self._create_arguments(app))
                if not re.fullmatch(r"[a-f0-9]{64}", container_id):
                    raise ValueError("Docker returned an invalid container ID; inspect the application status")
                app["container_id"] = container_id
                atomic_json(self.path, data)
            else:
                app["container_id"] = info["Id"]
            # Verify labels and image immediately before starting any existing or newly created container.
            if self._inspect_owned(app) is None:
                raise RuntimeError("Application container disappeared before start")
            self._checked(["start", app["container_id"]])
            result = self._status(app, self._inspect_owned(app))
            app.update(status=result["status"], started_at=now())
            atomic_json(self.path, data)
            return result

    def stop(self, app_id):
        with self.lock:
            data = self._load()
            app = self._find(data, app_id)
            info = self._inspect_owned(app)
            if info is not None:
                if info.get("State", {}).get("Running"):
                    self._checked(["stop", "--time", "10", info["Id"]], timeout=20)
                # Graceful stop must finish before removing the owned container.
                info = self._inspect_owned(app)
                if info is not None:
                    if info.get("State", {}).get("Running"):
                        raise RuntimeError("Application is still running; its state has been preserved")
                    self._checked(["rm", info["Id"]])
            app.update(container_id=None, container_name=None, status="stopped", stopped_at=now())
            atomic_json(self.path, data)
            return self._status(app, None)
