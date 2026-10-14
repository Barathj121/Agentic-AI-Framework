"""Durable, human-approved LangGraph orchestration of real sandbox computation."""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from filelock import FileLock, Timeout
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langsmith import tracing_context
from packaging.requirements import InvalidRequirement, Requirement

from workbench.artifacts import validate_and_register
from workbench.data import masked, sql_preview
from workbench.llm import OllamaClient
from workbench.prompts import BUILD_SYSTEM, PROPOSALS_SYSTEM, REPAIR_SYSTEM, UNDERSTANDING_SYSTEM
from workbench.sandbox import AVAILABLE_DEPENDENCIES, SandboxManager
from workbench.schemas import BuildResult, ExecutionResult, Proposal, ProposalSet, Understanding
from workbench.settings import Settings
from workbench.workspace import WorkspaceManager, safe_path


class GraphState(TypedDict, total=False):
    workspace_id: str
    run_id: str
    problem: str
    profile: dict
    understanding: dict
    exploration: list[dict]
    proposals: list[dict]
    selected_plan: dict
    instructions: str
    status: str
    build: dict
    execution: dict
    validation: dict
    attempt: int
    history: list[dict]
    error: str | None
    generation_error: str | None
    decision: str


class WorkflowCancelled(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _package_name(value: str) -> str:
    value = re.sub(r"[-_.]+", "-", value).lower()
    return {"sklearn": "scikit-learn"}.get(value, value)


def _dependency_issues(dependencies: list[str]) -> tuple[list[str], list[str]]:
    errors, limitations = [], []
    available = {_package_name(d) for d in AVAILABLE_DEPENDENCIES}
    for dependency in dependencies:
        try:
            requirement = Requirement(dependency)
        except InvalidRequirement:
            errors.append(f"Invalid dependency declaration: {dependency}")
            continue
        name = _package_name(requirement.name)
        if requirement.url or requirement.extras:
            errors.append(f"Package downloads and unlisted extras are unavailable: {dependency}")
        elif name not in available and requirement.name not in sys.stdlib_module_names:
            errors.append(f"Dependency is not installed in the sandbox image: {dependency}")
        elif requirement.specifier:
            limitations.append(f"The sandbox runner must verify the installed version for {dependency}.")
    return errors, limitations


def _memory_bytes(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(b|[kmgt](?:i?b)?)?\s*", str(value), re.I)
    if not match:
        return None
    unit = (match.group(2) or "b").lower()[0]
    return int(float(match.group(1)) * 1024 ** {"b": 0, "k": 1, "m": 2, "g": 3, "t": 4}[unit])


class Workflow:
    def __init__(self, settings: Settings, llm=None, sandbox=None):
        # Ambient LangSmith settings must not export private profiles, prompts,
        # generated source or results from this explicitly local-only workflow.
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        self.settings = settings
        self.manager = WorkspaceManager(settings.workspace_root)
        self.llm = llm if llm is not None else OllamaClient(settings)
        self.sandbox = sandbox if sandbox is not None else SandboxManager(settings)

    def _workspace(self, workspace: Path) -> Path:
        workspace = Path(workspace)
        expected = self.manager.get(workspace.name)
        if workspace.resolve() != expected.resolve():
            raise ValueError("Workspace is outside the configured workspace root")
        return expected

    @staticmethod
    def _lock(workspace: Path) -> FileLock:
        return FileLock(str(safe_path(workspace, "metadata/workflow.lock")), timeout=0)

    def _check_cancel(self, workspace: Path) -> None:
        if safe_path(workspace, "metadata/cancel.requested").exists():
            raise WorkflowCancelled("Cancelled by the user")

    def _stage(self, workspace: Path, state: GraphState, status: str, message: str, **updates) -> dict:
        if status not in {"CANCELLED", "REJECTED", "FAILED"}:
            self._check_cancel(workspace)
        self.manager.status(workspace, status, error=updates.get("error"))
        self.manager.event(workspace, status, message, attempt=updates.get("attempt", state.get("attempt", 0)))
        value = {**state, **updates, "status": status}
        self.manager.write(workspace, "metadata/state.json", value)
        return {**updates, "status": status}

    @contextmanager
    def _graph(self, workspace: Path):
        with tracing_context(enabled=False), SqliteSaver.from_conn_string(str(safe_path(workspace, "metadata/checkpoints.sqlite"))) as saver:
            graph = StateGraph(GraphState)
            nodes = {
                "understand": self._understand,
                "explore": self._explore,
                "proposals": self._proposals,
                "feasibility": self._feasibility,
                "approval": self._approval,
                "generate": self._generate,
                "execute": self._execute,
                "validate": self._validate,
                "repair": self._repair,
                "complete": self._complete,
                "failed": self._failed,
            }
            for name, function in nodes.items():
                # Bind each function separately; no late-bound loop closure.
                graph.add_node(name, lambda state, fn=function: fn(workspace, state))
            graph.add_edge(START, "understand")
            graph.add_edge("understand", "explore")
            graph.add_edge("explore", "proposals")
            graph.add_edge("proposals", "feasibility")
            graph.add_edge("feasibility", "approval")
            graph.add_conditional_edges("approval", lambda s: "generate" if s.get("decision") == "approve" else END)
            graph.add_conditional_edges("generate", self._after_build)
            graph.add_conditional_edges("execute", self._after_execute)
            graph.add_conditional_edges("validate", self._after_validate)
            graph.add_conditional_edges("repair", self._after_build)
            graph.add_edge("complete", END)
            graph.add_edge("failed", END)
            yield graph.compile(checkpointer=saver)

    def _config(self, workspace: Path) -> dict:
        run = self.manager.read(workspace, "metadata/workflow.json", {})
        if not run.get("run_id"):
            raise ValueError("This workspace has no proposal workflow to resume")
        return {"configurable": {"thread_id": f"{workspace.name}:{run['run_id']}"}, "recursion_limit": 40}

    def _invoke(self, workspace: Path, graph, value: dict | Command) -> dict:
        config = self._config(workspace)
        try:
            result = graph.invoke(value, config)
            result = {key: val for key, val in result.items() if key != "__interrupt__"}
            if result.get("status") != "COMPLETED":
                self._check_cancel(workspace)
            self.manager.write(workspace, "metadata/state.json", result)
            return result
        except Exception as exc:
            status = "CANCELLED" if isinstance(exc, WorkflowCancelled) else "FAILED"
            state = dict(graph.get_state(config).values)
            error = f"{type(exc).__name__}: {exc}"[:4000]
            state.update(self._stage(workspace, state, status, error, error=error))
            # Keep terminal outcome in the actual SQLite state as well as display metadata.
            graph.update_state(config, {"status": status, "error": error})
            return state

    def propose(self, workspace: Path, *, reset_cancellation: bool = True) -> dict:
        workspace = self._workspace(workspace)
        try:
            with self._lock(workspace):
                previous = self.manager.read(workspace, "metadata/state.json", {})
                flag = safe_path(workspace, "metadata/cancel.requested")
                if not reset_cancellation and flag.exists():
                    previous.update(self._stage(workspace, previous, "CANCELLED", "Cancelled before the queued proposal worker began"))
                    return previous
                if previous.get("status") == "AWAITING_APPROVAL":
                    return previous
                if previous.get("status") == "COMPLETED":
                    raise ValueError("Create a new workspace for a different approved solution")
                if reset_cancellation:
                    flag.unlink(missing_ok=True)
                metadata = self.manager.read(workspace, "metadata/workspace.json", {})
                profile = self.manager.read(workspace, "metadata/profile.json", {})
                if not profile or not profile.get("columns"):
                    raise ValueError("Ingest and profile a CSV before requesting proposals")
                run_id = uuid4().hex
                self.manager.write(workspace, "metadata/workflow.json", {"run_id": run_id, "started_at": _now()})
                initial: GraphState = {
                    "workspace_id": workspace.name,
                    "run_id": run_id,
                    "problem": metadata.get("problem", ""),
                    "profile": profile,
                    "status": "DATA_PROFILED",
                    "attempt": 0,
                    "history": [],
                    "error": None,
                }
                with self._graph(workspace) as graph:
                    return self._invoke(workspace, graph, initial)
        except Timeout as exc:
            raise RuntimeError("A workflow is already running for this workspace") from exc

    def approve(self, workspace: Path, proposal_id: str, instructions: str = "") -> dict:
        workspace = self._workspace(workspace)
        if len(instructions) > 4000:
            raise ValueError("Additional instructions are limited to 4,000 characters")
        try:
            with self._lock(workspace), self._graph(workspace) as graph:
                self._check_cancel(workspace)
                snapshot = graph.get_state(self._config(workspace))
                state = snapshot.values
                if state.get("status") != "AWAITING_APPROVAL" or "approval" not in snapshot.next:
                    raise ValueError("The workflow is not awaiting approval")
                proposal = next((p for p in state.get("proposals", []) if p["proposal_id"] == proposal_id), None)
                if proposal is None:
                    raise ValueError("Unknown proposal ID")
                if proposal["feasibility"] == "NOT_FEASIBLE":
                    raise ValueError("A NOT_FEASIBLE proposal cannot be approved")
                return self._invoke(workspace, graph, Command(resume={
                    "action": "approve", "proposal_id": proposal_id, "instructions": instructions,
                }))
        except Timeout as exc:
            raise RuntimeError("A workflow is already running for this workspace") from exc

    def reject(self, workspace: Path, reason: str = "") -> dict:
        workspace = self._workspace(workspace)
        try:
            with self._lock(workspace), self._graph(workspace) as graph:
                snapshot = graph.get_state(self._config(workspace))
                if snapshot.values.get("status") != "AWAITING_APPROVAL" or "approval" not in snapshot.next:
                    raise ValueError("The workflow is not awaiting approval")
                return self._invoke(workspace, graph, Command(resume={"action": "reject", "reason": reason[:4000]}))
        except Timeout as exc:
            raise RuntimeError("A workflow is already running for this workspace") from exc

    def cancel(self, workspace: Path) -> None:
        workspace = self._workspace(workspace)
        if self.manager.read(workspace, "metadata/state.json", {}).get("status") == "COMPLETED":
            return
        # This flag must never wait for the workflow lock: a running node owns it.
        safe_path(workspace, "metadata/cancel.requested").touch()
        try:
            self.sandbox.cancel(workspace)
        finally:
            try:
                with self._lock(workspace):
                    state = self.manager.read(workspace, "metadata/state.json", {})
                    if state.get("status") == "COMPLETED":
                        return
                    state.update(self._stage(workspace, state, "CANCELLED", "Cancellation requested by user"))
                    run = self.manager.read(workspace, "metadata/workflow.json", {})
                    if run:
                        with self._graph(workspace) as graph:
                            graph.update_state(self._config(workspace), {"status": "CANCELLED", "decision": "cancel"})
            except Timeout:
                # The running worker checks the flag between every stage and after
                # local inference. Sandbox.cancel terminates an active container.
                pass

    def snapshot(self, workspace: Path) -> dict:
        workspace = self._workspace(workspace)
        state = self.manager.read(workspace, "metadata/state.json", {})
        state["cancellation_requested"] = safe_path(workspace, "metadata/cancel.requested").exists()
        return state

    def _bounded_profile(self, profile: dict) -> dict:
        columns = profile.get("columns", [])
        # Column names are an essential contract: never silently omit them.
        result = {
            "row_count": profile.get("row_count"),
            "columns": [{k: c[k] for k in ("name", "type", "null_count", "distinct_count") if k in c} for c in columns],
            "warnings": profile.get("warnings", [])[:8],
            "samples": profile.get("samples", [])[:3],
        }
        if len(_json(result)) > 7000:
            result.pop("samples", None)
            result["columns"] = [{"name": c["name"], "type": c.get("type")} for c in columns]
        return result

    def _capabilities(self) -> dict:
        return {
            "cpu_only": True,
            "cpus": self.settings.sandbox_cpus,
            "memory_limit": self.settings.sandbox_memory,
            "timeout_seconds": self.settings.sandbox_timeout_seconds,
            "network": False,
            "installed_packages": sorted(AVAILABLE_DEPENDENCIES),
            "source_table": "raw.source",
        }

    def _understand(self, workspace: Path, state: GraphState) -> dict:
        self._stage(workspace, state, "UNDERSTANDING", "Understanding the problem and dataset profile")
        value = self.llm.structured(Understanding, UNDERSTANDING_SYSTEM, _json({
            "problem": state["problem"], "profile": self._bounded_profile(state["profile"]),
        }))
        self._check_cancel(workspace)
        value = Understanding.model_validate(value)
        columns = {c["name"] for c in state["profile"]["columns"]}
        missing = sorted(set(value.relevant_columns) - columns)
        if missing:
            raise ValueError("Data understanding referenced columns absent from the actual schema: " + ", ".join(missing))
        self.manager.write(workspace, "metadata/understanding.json", value.model_dump(mode="json"))
        return {"understanding": value.model_dump(mode="json"), "status": "UNDERSTANDING"}

    def _explore(self, workspace: Path, state: GraphState) -> dict:
        self._stage(workspace, state, "EXPLORING", "Running up to three bounded read-only exploration queries")
        exploration = []
        for query in state["understanding"].get("exploration_queries", [])[:3]:
            self._check_cancel(workspace)
            if len(query) > 4000:
                exploration.append({"sql": query[:100], "error": "Query exceeds 4,000 character limit"})
                continue
            try:
                result = sql_preview(workspace, query, limit=20)
                columns = result.get("columns", [])
                rows = []
                for row in result.get("rows", [])[:20]:
                    if isinstance(row, dict):
                        rows.append({str(k): self._mask(str(k), v) for k, v in row.items()})
                    else:
                        rows.append([self._mask(str(columns[i]) if i < len(columns) else "", v) for i, v in enumerate(row)])
                exploration.append({"sql": query, "columns": columns, "rows": rows, "truncated": result.get("truncated", False)})
            except Exception as exc:
                exploration.append({"sql": query, "error": str(exc)[:600]})
            self.manager.event(workspace, "EXPLORING", "Exploration query completed", query_index=len(exploration), failed="error" in exploration[-1])
        self.manager.write(workspace, "metadata/exploration.json", exploration)
        return {"exploration": exploration, "status": "EXPLORING"}

    @staticmethod
    def _mask(column: str, value: Any) -> Any:
        return masked(value, column)

    def _proposals(self, workspace: Path, state: GraphState) -> dict:
        self._stage(workspace, state, "GENERATING_PROPOSALS", "Designing feasible computational approaches")
        exploration = state.get("exploration", [])
        # Keep each query's evidence bounded, preserving valid JSON throughout.
        evidence = [{**q, "rows": q.get("rows", [])[:5]} for q in exploration]
        value = self.llm.structured(ProposalSet, PROPOSALS_SYSTEM, _json({
            "problem": state["problem"],
            "profile": self._bounded_profile(state["profile"]),
            "understanding": state["understanding"],
            "exploration_evidence": evidence,
            "capabilities": self._capabilities(),
        }))
        self._check_cancel(workspace)
        value = ProposalSet.model_validate(value)
        return {"proposals": [p.model_dump(mode="json") for p in value.proposals], "status": "GENERATING_PROPOSALS"}

    def _output_issue(self, workspace: Path, output) -> str | None:
        if output.type in {"database_table", "database_view"}:
            if not re.fullmatch(r"(?:results|staging|features|audit)\.[A-Za-z_][A-Za-z0-9_]*", output.location):
                return f"Invalid derived database output: {output.location}"
        else:
            try:
                path = safe_path(workspace, output.location)
                relative = path.relative_to(workspace.resolve())
                if relative.parts[0] not in {"results", "models", "notebooks"}:
                    return f"Output must be in results/, models/ or notebooks/: {output.location}"
            except (ValueError, IndexError) as exc:
                return f"Unsafe output path {output.location}: {exc}"
        return None

    def _feasibility(self, workspace: Path, state: GraphState) -> dict:
        self._stage(workspace, state, "EVALUATING_FEASIBILITY", "Checking actual columns, packages, outputs and resource limits")
        columns = {c["name"] for c in state["profile"]["columns"]}
        ids = [p["proposal_id"] for p in state["proposals"]]
        proposals, checks = [], []
        for raw in state["proposals"]:
            proposal = Proposal.model_validate(raw)
            errors, limitations = _dependency_issues(proposal.required_dependencies)
            missing = sorted(set(proposal.required_columns) - columns)
            if missing:
                errors.append("Required columns are missing: " + ", ".join(missing))
            if ids.count(proposal.proposal_id) > 1:
                errors.append("Proposal IDs must be unique")
            if not proposal.expected_outputs:
                errors.append("At least one inspectable expected output is required")
            locations = [o.location for o in proposal.expected_outputs]
            if len(locations) != len(set(locations)):
                errors.append("Expected output locations must be unique")
            for output in proposal.expected_outputs:
                issue = self._output_issue(workspace, output)
                if issue:
                    errors.append(issue)
            memory = _memory_bytes(proposal.resource_estimate.memory)
            limit = _memory_bytes(self.settings.sandbox_memory)
            if memory and limit and memory > limit:
                errors.append(f"Estimated memory {proposal.resource_estimate.memory} exceeds {self.settings.sandbox_memory}")
            cpu = str(proposal.resource_estimate.cpu).lower()
            if "gpu" in cpu or "cuda" in cpu:
                errors.append("The sandbox provides CPU computation only")
            cores = re.fullmatch(r"\s*(\d+)\s*(?:cores?|cpus?)\s*", cpu)
            if cores and int(cores.group(1)) > self.settings.sandbox_cpus:
                errors.append("Estimated CPU requirement exceeds the configured CPU limit")
            seconds = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(s|sec|seconds?|min|minutes?|h|hours?)\s*", str(proposal.resource_estimate.execution_time).lower())
            if seconds:
                multiplier = 3600 if seconds.group(2).startswith("h") else 60 if seconds.group(2).startswith("m") else 1
                if float(seconds.group(1)) * multiplier > self.settings.sandbox_timeout_seconds:
                    errors.append("Estimated execution time exceeds the configured sandbox timeout")
            if str(proposal.resource_estimate.memory).lower() == "high" or cpu == "high":
                limitations.append("High qualitative resource estimate; execution may reach the enforced CPU/memory limits.")
            feasibility = "NOT_FEASIBLE" if errors else proposal.feasibility
            if limitations and feasibility == "FEASIBLE":
                feasibility = "FEASIBLE_WITH_LIMITATIONS"
            additions = [*errors, *limitations]
            proposal = proposal.model_copy(update={
                "feasibility": feasibility,
                "limitations": list(dict.fromkeys([*proposal.limitations, *additions])),
                "confidence_explanation": proposal.confidence_explanation + (
                    " Host checks: " + "; ".join(additions) if additions else
                    " Host checks confirmed required columns, declared dependencies and output paths; runtime behavior remains unverified."
                ),
            })
            proposals.append(proposal.model_dump(mode="json"))
            checks.append({"proposal_id": proposal.proposal_id, "passed": not errors, "errors": errors, "limitations": limitations})
        self.manager.write(workspace, "metadata/proposals.json", {"proposals": proposals})
        self.manager.write(workspace, "metadata/feasibility.json", {"checks": checks, "capabilities": self._capabilities()})
        return self._stage(workspace, state, "AWAITING_APPROVAL", "Proposals saved; execution is paused until explicit approval", proposals=proposals)

    def _approval(self, workspace: Path, state: GraphState) -> dict:
        # No side effects before interrupt: this node is replayed when resumed.
        response = interrupt({"kind": "solution_approval", "workspace_id": workspace.name, "proposals": state["proposals"]})
        if not isinstance(response, dict) or response.get("action") not in {"approve", "reject", "cancel"}:
            raise ValueError("Invalid approval response")
        if response["action"] != "approve":
            status = "REJECTED" if response["action"] == "reject" else "CANCELLED"
            self.manager.write(workspace, "metadata/decision.json", {**response, "decided_at": _now()})
            return self._stage(workspace, state, status, str(response.get("reason") or status.title()), decision=response["action"])
        self._check_cancel(workspace)
        selected = next((p for p in state["proposals"] if p["proposal_id"] == response.get("proposal_id")), None)
        if selected is None or selected["feasibility"] == "NOT_FEASIBLE":
            raise ValueError("The selected proposal is unavailable or not feasible")
        plan = {"proposal": selected, "instructions": response.get("instructions", ""), "approved_at": _now()}
        self.manager.write(workspace, "metadata/selected_plan.json", plan)
        return self._stage(workspace, state, "PLAN_APPROVED", "User approved the selected implementation plan", selected_plan=plan, instructions=plan["instructions"], decision="approve")

    def _build_context(self, state: GraphState) -> dict:
        return {
            "problem": state["problem"],
            "profile": self._bounded_profile(state["profile"]),
            "approved_plan": state["selected_plan"],
            "capabilities": self._capabilities(),
        }

    def _check_build(self, workspace: Path, state: GraphState, value: BuildResult) -> BuildResult:
        value = BuildResult.model_validate(value)
        approved = Proposal.model_validate(state["selected_plan"]["proposal"])
        def contract(output):
            return output.model_dump(exclude={"description"}, mode="json")
        expected = {o.location: contract(o) for o in approved.expected_outputs}
        actual = {o.location: contract(o) for o in value.expected_outputs}
        if len(value.expected_outputs) != len(actual) or expected != actual:
            raise ValueError("Generated outputs changed the approved contract; reapproval is required for changed objectives")
        errors, _ = _dependency_issues(value.required_dependencies)
        if errors:
            raise ValueError("; ".join(errors))
        if value.entrypoint != "generated/solution.py":
            raise ValueError("The entrypoint must be generated/solution.py")
        if not value.files or len(value.files) > 30:
            raise ValueError("A build must contain 1 to 30 source files")
        paths = [f.path for f in value.files]
        if len(paths) != len(set(paths)) or value.entrypoint not in paths:
            raise ValueError("Generated files must be unique and include the declared entrypoint")
        if sum(len(f.content.encode("utf-8")) for f in value.files) > 256_000:
            raise ValueError("Generated source exceeds the 256 KB build limit")
        for file in value.files:
            path = safe_path(workspace, file.path)
            if path.relative_to(workspace.resolve()).parts[0] != "generated":
                raise ValueError("Source files must be beneath generated/")
            if path.suffix.lower() not in {".py", ".sql", ".json", ".txt", ".md", ".yaml", ".yml"}:
                raise ValueError("Unsupported generated source extension")
            if path.suffix.lower() == ".py":
                ast.parse(file.content, filename=file.path)
        return value.model_copy(update={"expected_outputs": approved.expected_outputs})

    def _write_build(self, workspace: Path, state: GraphState, build: BuildResult, attempt: int) -> None:
        # Preserve every attempted complete source contract before replacing working files.
        self.manager.write(workspace, f"metadata/attempts/{attempt}/build.json", build.model_dump(mode="json"))
        previous_paths = {f["path"] for f in state.get("build", {}).get("files", [])}
        current_paths = {f.path for f in build.files}
        for removed in previous_paths - current_paths:
            path = safe_path(workspace, removed)
            if path.relative_to(workspace.resolve()).parts[0] == "generated":
                path.unlink(missing_ok=True)
        for source in build.files:
            path = safe_path(workspace, source.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as handle:
                handle.write(source.content)
                temporary = Path(handle.name)
            os.replace(temporary, path)
        self.manager.write(workspace, "metadata/build.json", build.model_dump(mode="json"))

    def _generate(self, workspace: Path, state: GraphState) -> dict:
        self._stage(workspace, state, "GENERATING_SOLUTION", "Generating reusable source files for the approved plan")
        value = self.llm.structured(BuildResult, BUILD_SYSTEM, _json(self._build_context(state)))
        self._check_cancel(workspace)
        return self._prepare_build(workspace, state, value, 0, "GENERATING_SOLUTION")

    def _prepare_build(self, workspace: Path, state: GraphState, value: BuildResult, attempt: int, status: str) -> dict:
        # Invalid code and output-contract drift are real, bounded repair inputs.
        # They are not described as container execution or fabricated exit codes.
        value = BuildResult.model_validate(value)
        self.manager.write(workspace, f"metadata/attempts/{attempt}/build.json", value.model_dump(mode="json"))
        try:
            build = self._check_build(workspace, state, value)
        except (ValueError, SyntaxError) as exc:
            error = f"{type(exc).__name__}: {exc}"[:3000]
            self.manager.write(workspace, f"metadata/attempts/{attempt}/generation_error.json", {"error": error})
            self.manager.event(workspace, status, "Generated source failed pre-execution checks", attempt=attempt, error=error)
            return {"build": value.model_dump(mode="json"), "attempt": attempt, "status": status,
                    "generation_error": error, "validation": {},
                    "history": [*state.get("history", []), {"attempt": attempt, "generation_error": error}]}
        self._write_build(workspace, state, build, attempt)
        self.manager.event(workspace, status, "Source files saved; no generated code has run on the host", files=[f.path for f in build.files])
        return {"build": build.model_dump(mode="json"), "attempt": attempt, "status": status,
                "generation_error": None, "validation": {}}

    def _after_build(self, state: GraphState) -> str:
        if state.get("generation_error"):
            return "repair" if state["attempt"] < self.settings.max_repair_attempts else "failed"
        return "execute"

    def _execute(self, workspace: Path, state: GraphState) -> dict:
        self._stage(workspace, state, "EXECUTING", "Executing generated code in the isolated Docker sandbox", attempt=state["attempt"])
        result = self.sandbox.execute(workspace, BuildResult.model_validate(state["build"]), state["attempt"])
        result = ExecutionResult.model_validate(result)
        evidence = {**result.model_dump(mode="json"), "attempt": state["attempt"]}
        self.manager.write(workspace, "metadata/execution.json", evidence)
        self.manager.write(workspace, f"metadata/attempts/{state['attempt']}/execution.json", evidence)
        if result.cancelled:
            raise WorkflowCancelled("Sandbox execution cancelled")
        self._check_cancel(workspace)
        history = [*state.get("history", []), {"attempt": state["attempt"], "execution": evidence}]
        return {"execution": result.model_dump(mode="json"), "history": history, "status": "EXECUTING"}

    def _after_execute(self, state: GraphState) -> str:
        if state["execution"]["success"]:
            return "validate"
        if state["execution"].get("exit_code") is None and not any(
            state["execution"].get(flag) for flag in ("timed_out", "oom_killed")
        ):
            # No container process ran. Rewriting source cannot repair an
            # unavailable Docker engine/image or an invalid host configuration.
            return "failed"
        return "repair" if state["attempt"] < self.settings.max_repair_attempts else "failed"

    def _validate(self, workspace: Path, state: GraphState) -> dict:
        self._stage(workspace, state, "VALIDATING", "Checking actual output files, database objects and declared constraints")
        result = validate_and_register(
            workspace, BuildResult.model_validate(state["build"]),
            ExecutionResult.model_validate(state["execution"]), self.settings, self.sandbox,
        )
        self._check_cancel(workspace)
        self.manager.write(workspace, "metadata/validation.json", result)
        self.manager.write(workspace, f"metadata/attempts/{state['attempt']}/validation.json", result)
        history = list(state.get("history", []))
        if history:
            history[-1] = {**history[-1], "validation": result}
        return {"validation": result, "history": history, "status": "VALIDATING"}

    def _after_validate(self, state: GraphState) -> str:
        if state["validation"].get("passed"):
            return "complete"
        return "repair" if state["attempt"] < self.settings.max_repair_attempts else "failed"

    def _log_excerpt(self, workspace: Path, relative: str | None, limit: int = 3000) -> str:
        if not relative:
            return ""
        path = safe_path(workspace, relative)
        if not path.is_file():
            return ""
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - limit))
            return handle.read(limit).decode("utf-8", errors="replace")

    def _repair(self, workspace: Path, state: GraphState) -> dict:
        attempt = state["attempt"] + 1
        self._stage(workspace, state, "REPAIRING", "Repairing source using actual failure evidence", attempt=attempt)
        context = self._build_context(state)
        # Keep all approved obligations and complete source, while avoiding the
        # duplicated output contract and profile sample statistics in a repair.
        context["profile"] = {"row_count": state["profile"].get("row_count"),
                              "columns": [{"name": c["name"], "type": c.get("type")} for c in state["profile"]["columns"]]}
        context.update({
            "current_files": state["build"]["files"],
            "current_validation_checks": state["build"].get("validation_checks", []),
            "generation_error": state.get("generation_error"),
            "execution": state.get("execution", {}),
            "validation": state.get("validation", {}),
            "stderr_tail": self._log_excerpt(workspace, state.get("execution", {}).get("stderr_path"), limit=2000),
            "stdout_tail": self._log_excerpt(workspace, state.get("execution", {}).get("stdout_path"), limit=500),
            "attempt": attempt,
            "previous_attempts": [{"attempt": h["attempt"], "error": h.get("execution", {}).get("error") or h.get("generation_error"), "exit_code": h.get("execution", {}).get("exit_code")} for h in state.get("history", [])],
        })
        value = self.llm.structured(BuildResult, REPAIR_SYSTEM, _json(context))
        self._check_cancel(workspace)
        return self._prepare_build(workspace, state, value, attempt, "REPAIRING")

    def _complete(self, workspace: Path, state: GraphState) -> dict:
        self._check_cancel(workspace)
        manifest = self.manager.read(workspace, "metadata/manifest.json", {})
        manifest.update({
            "workspace_id": workspace.name,
            "status": "COMPLETED",
            "selected_proposal_id": state["selected_plan"]["proposal"]["proposal_id"],
            "artifacts": state["validation"].get("artifacts", []),
            "completed_at": _now(),
        })
        self.manager.write(workspace, "metadata/manifest.json", manifest)
        return self._stage(workspace, state, "COMPLETED", "Real execution and output validation completed; artifacts registered", error=None)

    def _failed(self, workspace: Path, state: GraphState) -> dict:
        errors = state.get("validation", {}).get("errors", [])
        error = state.get("generation_error") or ("; ".join(str(e) for e in errors) if errors else state.get("execution", {}).get("error"))
        error = error or "Execution or validation failed after the allowed repair attempts"
        return self._stage(workspace, state, "FAILED", error[:1000], error=error[:4000])
