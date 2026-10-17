"""Additive LangGraph agent: plan → financial tools → verify → grounded draft.

This path executes reviewed functions, never LLM-generated code or Docker.
The existing workbench Workflow and its local-only model policy are unchanged.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from .models import GeminiAPIReasoner, LocalReasoner, RulesReasoner, VertexReasoner
from .schemas import Finding, InvestigationInput, Narrative, Plan
from .tools import TOOLS, ref

VERSION = "1.0.0"


class State(TypedDict, total=False):
    source: InvestigationInput
    plan: Plan
    findings: list[Finding]
    narrative: Narrative
    trace: list[dict]


def source_ids(source):
    ids = set()
    for table, key in (("employees", "employee_id"), ("accounts", "account_id"),
                       ("relationships", "relationship_id"), ("trades", "trade_id"),
                       ("events", "event_id"), ("entities", "entity_id"), ("payments", "transaction_id")):
        ids.update(ref(table, getattr(row, key)) for row in getattr(source, table))
    return ids


class ClariFiAgent:
    def __init__(self, *, mode="vertex", reasoner=None, model=None):
        if mode not in {"vertex", "gemini", "local", "rules"}:
            raise ValueError("ClariFi mode must be vertex, gemini, local or rules")
        self.reasoner = reasoner if reasoner is not None else (
            VertexReasoner(model) if mode == "vertex" else GeminiAPIReasoner(model) if mode == "gemini"
            else LocalReasoner(model) if mode == "local" else RulesReasoner())

    def _plan(self, state):
        source = state["source"]
        context = {"available_tools": source.tools,
                   "table_counts": {key: len(getattr(source, key)) for key in
                                    ("employees", "accounts", "relationships", "trades", "events", "entities", "payments")}}
        plan = Plan.model_validate(self.reasoner.plan(context))
        if len(set(plan.tools)) != len(plan.tools) or set(plan.tools) != set(source.tools):
            raise ValueError("Planner must select each available reviewed tool exactly once")
        return {"plan": plan, "trace": [{"stage": "plan", "tools": plan.tools}]}

    def _investigate(self, state):
        findings = []
        trace = list(state["trace"])
        for tool in state["plan"].tools:
            started = time.monotonic()
            findings.extend(TOOLS[tool](state["source"]))
            if len(findings) > 2000:
                raise ValueError("Investigation produces too many identity candidates; narrow the supplied scope")
            trace.append({"stage": "tool", "tool": tool, "duration_seconds": round(time.monotonic() - started, 4)})
        return {"findings": sorted(findings, key=lambda f: (-f.score, f.finding_id)), "trace": trace}

    def _verify(self, state):
        allowed = source_ids(state["source"])
        ids = [f.finding_id for f in state["findings"]]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate findings")
        for finding in state["findings"]:
            if not set(finding.evidence_ids) <= allowed:
                raise ValueError("Finding cites unknown source evidence")
            if finding.score != max(0, min(100, sum(s.points for s in finding.signals))):
                raise ValueError("Score does not reconcile to signal contributions")
            if any(not set(s.evidence_ids) <= set(finding.evidence_ids) for s in finding.signals):
                raise ValueError("Signal cites evidence absent from finding")
        return {"trace": state["trace"] + [{"stage": "verify", "passed": True,
                                          "checks": ["unique_findings", "source_references", "score_reconciliation"]}]}

    def _summarize(self, state):
        # Bound prompt size and disclose omitted findings. Identifiers replace names,
        # addresses, account details and relationship source text in model context.
        selected = state["findings"][:25]
        context = {"findings": [{"finding_id": f.finding_id, "score": f.score,
                                 "disposition": f.disposition, "gaps": f.gaps,
                                 "signals": [{"name": s.name, "points": s.points} for s in f.signals],
                                 "evidence_ids": f.evidence_ids[:20]}
                                for f in selected],
                   "total_findings": len(state["findings"]), "drafted_findings": len(selected)}
        narrative = Narrative.model_validate(self.reasoner.summarize(context))
        by_id = {f["finding_id"]: set(f["evidence_ids"]) for f in context["findings"]}
        claimed = [c.finding_id for c in narrative.claims]
        if len(set(claimed)) != len(claimed) or set(claimed) != set(by_id):
            raise ValueError("Narrative must cover each selected finding once")
        if any(not set(c.evidence_ids) <= by_id[c.finding_id] for c in narrative.claims):
            raise ValueError("Narrative cites evidence outside the associated finding")
        return {"narrative": narrative, "trace": state["trace"] + [{"stage": "draft", "findings": len(selected)}]}

    def run(self, payload: dict | InvestigationInput, *, approved=False) -> dict:
        if approved is not True:
            raise ValueError("Explicit investigation approval is required")
        source = InvestigationInput.model_validate(payload)
        graph = StateGraph(State)
        for name, action in (("plan_investigation", self._plan), ("run_tools", self._investigate),
                             ("verify_evidence", self._verify), ("draft_report", self._summarize)):
            graph.add_node(name, action)
        graph.add_edge(START, "plan_investigation")
        graph.add_edge("plan_investigation", "run_tools")
        graph.add_edge("run_tools", "verify_evidence")
        graph.add_edge("verify_evidence", "draft_report")
        graph.add_edge("draft_report", END)
        # Avoid ambient LangSmith export of financial data from this agent.
        with tracing_context(enabled=False):
            state = graph.compile().invoke({"source": source})
        digest = hashlib.sha256(json.dumps(source.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
        return {
            "investigation_id": uuid4().hex, "case_id": source.case_id, "agent": "clarifi",
            "version": VERSION, "status": "COMPLETED", "created_at": datetime.now(timezone.utc).isoformat(),
            "input_sha256": digest, "plan": state["plan"].model_dump(),
            "findings": [f.model_dump(mode="json") for f in state["findings"]],
            "narrative": state["narrative"].model_dump(), "trace": state["trace"],
            "provenance": {"reasoning_provider": self.reasoner.provider, "model": self.reasoner.model,
                           "ai_used": self.reasoner.provider in {"vertex_ai_gemini", "gemini_api", "local_ollama"},
                           "analysis": "reviewed Python tools; no generated code", "analyst_review_required": True,
                           "score_kind": "heuristic triage, not calibrated probability",
                           "ml": "retrospective per-currency/issuer Isolation Forest; minimum 20 rows and 3 distinct notionals",
                           "ml_scored_findings": sum(f.anomaly_percentile is not None for f in state["findings"]),
                           "accuracy": "unmeasured; labeled held-out validation required",
                           "narrative_scope": "top 25 findings; full findings retained",
                           "narrative_validation": "schema, coverage and references only; semantic correctness requires analyst review"},
        }
