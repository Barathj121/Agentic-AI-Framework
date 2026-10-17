"""Google ADK runtime with a bounded, explicitly local model adapter.

BaseLlm is ADK's supported model extension point. Reusing the existing HTTP
transports preserves local-only URLs, response limits and proxy isolation. No
Gemini client, automatic provider selection, or generated host tools are used.
"""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

# Do not export private local prompts through ambient OpenTelemetry settings.
os.environ["OTEL_SDK_DISABLED"] = "true"

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.run_config import RunConfig
from google.adk.events import Event, EventActions
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field


def run_sync(factory):
    """Run an async ADK invocation from Streamlit, CLI, or an async caller."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="workbench-adk") as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()


class LocalModel(BaseLlm):
    """ADK model adapter for our native Ollama/local OpenAI HTTP clients."""

    transport: Any = Field(exclude=True, repr=False)
    response_schema: dict
    schema_name: str

    async def generate_content_async(self, llm_request, stream=False):
        if llm_request.config and llm_request.config.tools:
            raise ValueError("This local adapter uses structured reasoning only; approved tools are invoked deterministically.")
        messages = []
        instruction = llm_request.config.system_instruction if llm_request.config else None
        if instruction:
            text = instruction if isinstance(instruction, str) else "\n".join(p.text or "" for p in instruction.parts or [])
            messages.append({"role": "system", "content": text})
        for content in llm_request.contents or []:
            if any(p.function_call or p.function_response for p in content.parts or []):
                raise ValueError("Function calls are not enabled for this local reasoning adapter.")
            text = "\n".join(p.text or "" for p in content.parts or [])
            messages.append({"role": "assistant" if content.role == "model" else "user", "content": text})
        try:
            raw, reason = await asyncio.to_thread(
                self.transport._generate, self.schema_name, self.response_schema, messages,
            )
        except Exception as exc:
            yield LlmResponse(error_code=type(exc).__name__, error_message=str(exc))
            return
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=raw)]),
            partial=False, turn_complete=True,
            custom_metadata={"provider_finish_reason": reason, "provider": self.transport.provider, "model": self.model},
        )


def responsibility(schema_name: str, system: str = "") -> str:
    name = schema_name.lower()
    if "mapping" in name:
        return "mapping_agent"
    if "understanding" in name:
        return "data_understanding_agent"
    if "build" in name or "transformation" in name:
        return "solution_builder_agent"
    if "proposal" in name or "plan" in name:
        return "solution_planning_agent"
    if "validation" in name:
        return "validation_agent"
    return "structured_reasoning_agent"


async def _structured_turn(transport, schema_name: str, schema: dict, messages: list[dict]):
    name = responsibility(schema_name)
    system = messages[0]["content"]
    # Callable instructions bypass ADK's {state_variable} template interpolation;
    # JSON schemas and source code can contain arbitrary literal braces.
    agent = LlmAgent(
        name=name,
        model=LocalModel(model=transport.model, transport=transport, response_schema=schema, schema_name=schema_name),
        instruction=lambda _ctx: system,
        include_contents="none",
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
        generate_content_config=types.GenerateContentConfig(temperature=transport.settings.llm_temperature),
    )
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="workbench_reasoning", user_id="local", session_id=uuid4().hex)
    runner = Runner(agent=agent, app_name="workbench_reasoning", session_service=sessions)
    text, reason, events = "", None, []
    async for event in runner.run_async(
        user_id="local", session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=messages[-1]["content"])]),
        run_config=RunConfig(max_llm_calls=1),
    ):
        events.append(event.model_dump(mode="json", exclude_none=True))
        if event.error_code:
            raise RuntimeError(f"ADK model error {event.error_code}: {event.error_message}")
        if event.content and not event.partial:
            text = "".join(part.text or "" for part in event.content.parts or [])
            reason = (event.custom_metadata or {}).get("provider_finish_reason")
    return text, reason, events


def structured_turn(transport, schema_name: str, schema: dict, messages: list[dict]):
    return run_sync(lambda: _structured_turn(transport, schema_name, schema, messages))


class WorkbenchAgent(BaseAgent):
    """ADK custom workflow agent executing deterministic, approval-gated stages.

    Each stage checkpoint is persisted by the driver. A new invocation resumes
    the approved state without relying on Streamlit's memory. Reasoning stages
    invoke LlmAgents; CSV, Docker, artifact validation and routing stay Python.
    """

    driver: Any = Field(exclude=True, repr=False)
    workspace: Any = Field(exclude=True)
    workflow_state: dict
    phase: str

    async def _run_async_impl(self, ctx):
        stage = self.phase
        while stage:
            self.driver._check_cancel(self.workspace)
            updates = await asyncio.to_thread(getattr(self.driver, "_" + stage), self.workspace, self.workflow_state)
            self.workflow_state.update(updates)
            self.driver._checkpoint(self.workspace, self.workflow_state, stage)
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                content=types.Content(role="model", parts=[types.Part(text=f"{stage}: {self.workflow_state.get('status')}")]),
                actions=EventActions(state_delta={"stage": stage, "status": self.workflow_state.get("status")}),
            )
            if stage in {"feasibility", "complete", "failed"}:
                return
            if stage in {"generate", "repair"}:
                stage = self.driver._after_build(self.workflow_state)
            elif stage == "execute":
                stage = self.driver._after_execute(self.workflow_state)
            elif stage == "validate":
                stage = self.driver._after_validate(self.workflow_state)
            else:
                stage = {"understand": "explore", "explore": "proposals", "proposals": "feasibility"}[stage]


async def run_workflow(driver, workspace, state: dict, phase: str):
    agent = WorkbenchAgent(name="data_workbench", driver=driver, workspace=workspace, workflow_state=state, phase=phase)
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="data_workbench", user_id="local", state={"status": state.get("status")})
    runner = Runner(agent=agent, app_name="data_workbench", session_service=sessions)
    async for event in runner.run_async(
        user_id="local", session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=f"Run approved workflow phase: {phase}")]),
    ):
        driver._record_adk_event(workspace, event.model_dump(mode="json", exclude_none=True))
    return agent.workflow_state
