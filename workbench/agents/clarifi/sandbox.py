"""ClariFi tools executed through the workbench's existing Docker lifecycle."""
from __future__ import annotations

import json
from uuid import uuid4

from filelock import FileLock

from workbench.sandbox import SandboxManager
from workbench.workspace import atomic_json, safe_path
from .preparation import digest
from .schemas import Finding


class FinancialSandbox:
    def __init__(self, settings, workspace, *, sandbox=None):
        self.workspace = workspace
        self.sandbox = sandbox or SandboxManager(settings)
        self.timeout = settings.sandbox_timeout_seconds

    def __call__(self, source, tools):
        if len(set(tools)) != len(tools) or set(tools) != set(source.tools):
            raise ValueError("Select each available reviewed financial tool exactly once")
        with FileLock(str(safe_path(self.workspace, "metadata/database.lock")), timeout=self.timeout + 30):
            self.sandbox._recover_cleanup(self.workspace)
            content = source.model_dump_json().encode()
            safe_path(self.workspace, "input/clarifi.json").write_bytes(content)
            output = safe_path(self.workspace, "results/clarifi-tools.json")
            output.unlink(missing_ok=True)
            execution = self.sandbox._run(self.workspace, {"tools": tools}, "clarifi_tools", "clarifi-" + uuid4().hex[:10])
            atomic_json(safe_path(self.workspace, "metadata/clarifi_execution.json"), execution.model_dump())
            if not execution.success:
                # No host fallback and no model summary of incomplete tool output.
                raise RuntimeError("ClariFi sandbox did not complete. Check Docker, rebuild the sandbox image, and inspect the workspace execution logs.")
            output = safe_path(self.workspace, "results/clarifi-tools.json")
            if not output.is_file() or output.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("Missing or oversized sandbox financial evidence")
            result = json.loads(output.read_bytes())
            if result.get("input_sha256") != digest(content) or result.get("tools") != tools:
                raise ValueError("Sandbox evidence does not match the approved input and tools")
            findings = result.get("findings")
            if not isinstance(findings, list) or len(findings) > 2000:
                raise ValueError("Invalid sandbox findings")
            # Only accepted schema data is consumed; raw stdout is never a report.
            trace = result.get("trace")
            if not isinstance(trace, list) or len(trace) != len(tools) or [item.get("tool") for item in trace] != tools:
                raise ValueError("Incomplete sandbox tool trace")
            return {"findings": [Finding.model_validate(item).model_dump(mode="json") for item in findings],
                    "trace": [{"stage": "tool", "tool": tool, "execution": "docker_sandbox"} for tool in tools]}
