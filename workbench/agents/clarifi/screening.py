"""Verified scores first; explicit, independently validated AI drafts afterward."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from .agent import ClariFiAgent
from .schemas import Finding, InvestigationInput, Narrative, Plan


def input_hash(source):
    return hashlib.sha256(json.dumps(source.model_dump(mode='json'), sort_keys=True).encode()).hexdigest()


def screen(payload, *, approved=False, tool_executor=None):
    if approved is not True:
        raise ValueError('Explicit screening approval is required')
    source = InvestigationInput.model_validate(payload)
    # No model instance or generated code participates in scoring or verification.
    agent = ClariFiAgent(mode='rules', tool_executor=tool_executor)
    plan = Plan(tools=source.tools, rationale='Run the reviewed financial checks selected by the supplied evidence tables.')
    state = {'source': source, 'plan': plan, 'trace': [{'stage': 'scope', 'tools': source.tools}]}
    state.update(agent._investigate(state))
    state.update(agent._verify(state))
    return {
        'investigation_id': uuid4().hex, 'case_id': source.case_id, 'agent': 'clarifi',
        'version': '1.1.0', 'status': 'SCREENED', 'created_at': datetime.now(timezone.utc).isoformat(),
        'input_sha256': input_hash(source), 'plan': plan.model_dump(),
        'findings': [finding.model_dump(mode='json') for finding in state['findings']],
        'narrative': {'claims': [], 'next_steps': []}, 'trace': state['trace'],
        'provenance': {
            'workflow': 'scores_first', 'reasoning_provider': 'reviewed_financial_tools', 'model': None,
            'execution': 'docker_sandbox' if tool_executor is not None else 'in_process',
            'ai_used': False, 'analysis': 'reviewed Python financial checks; no model called',
            'analyst_review_required': True, 'score_kind': 'heuristic triage, not calibrated probability',
            'accuracy': 'unmeasured; labeled held-out validation required',
            'ml_scored_findings': sum(f.anomaly_percentile is not None for f in state['findings']),
            'narrative_scope': 'not generated',
        },
    }


def draft(payload, screening, *, approved=False, mode='gemini', model=None, tool_executor=None):
    if approved is not True:
        raise ValueError('Explicit AI drafting approval is required')
    if mode not in {'gemini', 'vertex', 'local'}:
        raise ValueError('Choose an AI model for the analyst draft')
    source = InvestigationInput.model_validate(payload)
    if screening.get('status') != 'SCREENED' or screening.get('input_sha256') != input_hash(source):
        raise ValueError('Screening does not match this input; screen and review it again')
    # Recompute trusted findings before any model call, rather than trusting
    # edited saved reports or a client-supplied score/evidence bundle.
    verified = screen(source, approved=True, tool_executor=tool_executor)
    if verified['findings'] != screening['findings']:
        raise ValueError('Screening evidence changed; screen and review it again')
    agent = ClariFiAgent(mode=mode, model=model)
    state = {'source': source, 'findings': [Finding.model_validate(f) for f in verified['findings']],
             'plan': Plan.model_validate(verified['plan']), 'trace': verified['trace']}
    state.update(agent._summarize(state))
    result = copy.deepcopy(verified)
    result.update(investigation_id=uuid4().hex, status='COMPLETED',
                  created_at=datetime.now(timezone.utc).isoformat(),
                  narrative=Narrative.model_validate(state['narrative']).model_dump(), trace=state['trace'])
    result['provenance'].update(
        screening_id=screening['investigation_id'], reasoning_provider=agent.reasoner.provider,
        model=agent.reasoner.model, ai_used=True, analysis='reviewed Python checks; model-generated analyst draft',
        narrative_scope='top 25 findings; full findings retained',
        narrative_validation='schema, coverage and references verified; semantic correctness requires analyst review')
    return result
