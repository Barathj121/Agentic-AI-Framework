"""Private Cloud Run service for the registered ClariFi agent; no Docker required."""
from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, HTTPException, Request
from pydantic import Field, StrictBool, ValidationError
from starlette.concurrency import run_in_threadpool

from workbench.agents import AGENTS, run_agent
from .schemas import InvestigationInput, Record
from .storage import persist

app = FastAPI(title="ClariFi Financial Agents", version="1.0.0")
slots = asyncio.Semaphore(2)
MAX_BODY = 5 * 1024 * 1024


class InvestigationRequest(Record):
    data: InvestigationInput
    approved: StrictBool = Field(default=False)
    model: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._/-]+$")


@app.get("/healthz")
def health():
    # Liveness only; deliberately does not imply cloud model readiness.
    return {"status": "ok", "agent": "clarifi", "mode": os.getenv("CLARIFI_MODE", "vertex"),
            "model": configured_model()}


def configured_model():
    if os.getenv("CLARIFI_MODE", "vertex") == "gemini":
        return os.getenv("CLARIFI_API_MODEL", "gemini-3.5-flash-lite")
    return os.getenv("CLARIFI_GEMINI_MODEL", "")


@app.get("/agents")
def agents():
    return AGENTS


@app.get("/input-schema")
def input_schema():
    return InvestigationRequest.model_json_schema()


def execute(body):
    report = run_agent("clarifi", body.data, mode=os.getenv("CLARIFI_MODE", "vertex"), approved=body.approved, model=body.model)
    report["evidence_uri"] = persist(body.data, report)
    return report


@app.post("/investigations")
async def investigate(request: Request):
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > MAX_BODY:
            raise HTTPException(413, "Investigation exceeds 5 MiB; narrow the scope")
        chunks.extend(chunk)
    try:
        body = InvestigationRequest.model_validate_json(bytes(chunks))
    except ValidationError:
        # ValidationError strings can contain submitted PII; don't echo inputs.
        raise HTTPException(422, "Invalid investigation data; use the documented input schema") from None
    if body.approved is not True:
        raise HTTPException(409, "Explicit investigation approval is required")
    if body.model is not None and body.model != configured_model():
        raise HTTPException(409, "Selected model differs from the deployed model; review the current configuration")
    async with slots:
        try:
            return await run_in_threadpool(execute, body)
        except Exception:
            # No offline fallback after a cloud/model/verification/storage failure.
            raise HTTPException(502, "Investigation failed; check model configuration, input scope and evidence storage. No completed report was returned.") from None


def execute_draft(body):
    from .screening import screen, draft
    screening = screen(body.data, approved=body.approved)
    report = draft(body.data, screening, approved=body.approved,
                   mode=os.getenv('CLARIFI_MODE', 'vertex'), model=body.model)
    report['evidence_uri'] = persist(body.data, report)
    return report


@app.post('/drafts')
async def draft_investigation(request: Request):
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > MAX_BODY:
            raise HTTPException(413, 'Investigation exceeds 5 MiB; narrow the scope')
        chunks.extend(chunk)
    try:
        body = InvestigationRequest.model_validate_json(bytes(chunks))
    except ValidationError:
        raise HTTPException(422, 'Invalid investigation data; use the documented input schema') from None
    if body.approved is not True:
        raise HTTPException(409, 'Explicit AI drafting approval is required')
    if body.model is not None and body.model != configured_model():
        raise HTTPException(409, 'Selected model differs from the deployed model; review the current configuration')
    async with slots:
        try:
            return await run_in_threadpool(execute_draft, body)
        except Exception:
            raise HTTPException(502, 'AI draft did not complete; check model quota and evidence storage. Previously saved screening results remain available.') from None
