"""Explicit Codex-assisted showcase with reviewed source and real Docker execution.

This CLI adapter is separate from production model clients. It does not claim
local-LLM generation, use cloud inference, or execute analysis on the host.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.schemas import BuildResult, ProposalSet, Understanding
from workbench.service import Service
from workbench.settings import Settings
from workbench.workflow import Workflow

PROMPT = """Build an explainable transaction investigation workbench from this synthetic dataset. Find concentrated transfer bursts, repeated modest payments, unusually large amounts relative to each customer's own baseline, changes in spending, and accounts connected through common recipients. Rank customers for human review; retain exact supporting transaction IDs. Show a connected-money-flow diagram, activity timeline, explainable ranking and signal coverage. Save queryable tables, CSVs, a concise investigation brief, threshold sensitivity and a reproducible notebook. This is descriptive exploration of synthetic data, not a fraud prediction or accusation."""

def contract():
    source = (ROOT / 'examples/showcase/solution.py').read_text(encoding='utf-8')
    # Parse only. This never runs generated analysis outside Docker.
    compile(source, 'generated/solution.py', 'exec', dont_inherit=True)
    columns = ['transaction_id', 'customer_id', 'account_id', 'destination_account', 'transaction_timestamp', 'transaction_amount', 'transaction_type']
    outputs = []
    for name, required in {
        'customer_review': ['customer_id', 'review_rank', 'review_score', 'signal_count', 'review_reasons'],
        'transaction_evidence': ['transaction_id', 'customer_id', 'evidence_reasons'],
        'recipient_summary': ['destination_account', 'source_accounts', 'transfers'],
        'transfer_edges': ['account_id', 'destination_account', 'transfer_count'],
        'hourly_activity': ['customer_id', 'hour', 'hourly_transfers'],
        'daily_activity': ['day', 'customer_id', 'transfers'],
        'threshold_sensitivity': ['threshold_multiplier', 'customers_for_review'],
    }.items():
        outputs.append({'name': name.replace('_', ' ').title(), 'type': 'database_table', 'location': 'results.' + name, 'required_columns': required, 'min_rows': 1})
        outputs.append({'name': name.replace('_', ' ').title() + ' CSV', 'type': 'csv', 'location': 'results/' + name + '.csv', 'required_columns': required, 'min_rows': 1})
    for name in ['review_ranking', 'recipient_flow', 'activity_timeline', 'signal_coverage']:
        outputs.append({'name': name.replace('_', ' ').title(), 'type': 'chart', 'location': f'results/{name}_chart.json'})
    outputs += [{'name': 'Showcase summary and cases', 'type': 'json', 'location': 'results/showcase_summary.json'}, {'name': 'Investigation dashboard', 'type': 'json', 'location': 'results/dashboard.json'}, {'name': 'Investigation brief', 'type': 'text_report', 'location': 'results/investigation_brief.md'}]
    dependencies = ['duckdb', 'pandas', 'numpy', 'plotly']
    limitations = ['CODEX-ASSISTED SHOWCASE: reviewed assistant-authored source, not a local-model autonomous run.', 'Synthetic records only; review score is a heuristic and not a fraud probability.', 'Fixed clock hours and retrospective medians; thresholds require domain review.', 'No currency field is provided; amounts use synthetic comparable units.', 'Annotations are never loaded or used as features.']
    proposal = {'proposal_id': 'codex_assisted_showcase', 'title': 'CODEX-ASSISTED SHOWCASE · Explainable transaction investigation', 'description': 'Assistant-authored reviewed implementation executed through the normal approval, sandbox and artifact-validation pipeline.', 'approach': PROMPT, 'implementation_steps': ['Validate the 10,000-row synthetic input', 'Compute five transparent descriptive signals inside Docker', 'Rank customers and preserve transaction evidence', 'Render four interactive charts and threshold sensitivity', 'Validate persisted results and retain reproducible source'], 'required_columns': columns, 'expected_outputs': outputs, 'advantages': ['Fast reproducible demonstration independent of local-model generation speed', 'Every case has transaction IDs, explicit rules and queryable evidence'], 'limitations': limitations, 'assumptions': ['All rows come from the bundled synthetic generator', 'Amounts use comparable synthetic units; timestamps are UTC'], 'resource_estimate': {'cpu': 'low', 'memory': 'under 1 GB expected', 'execution_time': 'under one minute expected'}, 'validation_strategy': ['Exact input row count and unique IDs', 'Coverage of every customer', 'Every evidence ID joins to raw input', 'All transfer edges reconcile to raw transfer count', 'Review scores exactly equal selected signal counts times 20', 'Charts and summary parse as JSON'], 'feasibility': 'FEASIBLE_WITH_LIMITATIONS', 'confidence_explanation': 'Deterministic reviewed analysis; actual success is determined only by Docker execution and output checks.', 'required_dependencies': dependencies}
    assertions = [
        ('10,000 unique synthetic input rows', 'SELECT count(*)=10000 AND count(DISTINCT transaction_id)=10000 FROM raw.source'),
        ('Customer coverage', 'SELECT (SELECT count(*) FROM results.customer_review)=(SELECT count(DISTINCT customer_id) FROM raw.source)'),
        ('Evidence identifiers join raw input', 'SELECT NOT EXISTS (SELECT 1 FROM results.transaction_evidence e LEFT JOIN raw.source r USING(transaction_id) WHERE r.transaction_id IS NULL)'),
        ('Transfer graph reconciles', "SELECT (SELECT sum(transfer_count) FROM results.transfer_edges)=(SELECT count(*) FROM raw.source WHERE transaction_type='transfer')"),
        ('Scores are rule counts', 'SELECT bool_and(review_score=signal_count*20 AND review_score BETWEEN 0 AND 100) FROM results.customer_review'),
        ('Sensitivity is monotone', 'SELECT (SELECT customers_for_review FROM results.threshold_sensitivity WHERE threshold_multiplier=0.8)>=(SELECT customers_for_review FROM results.threshold_sensitivity WHERE threshold_multiplier=1.0) AND (SELECT customers_for_review FROM results.threshold_sensitivity WHERE threshold_multiplier=1.0)>=(SELECT customers_for_review FROM results.threshold_sensitivity WHERE threshold_multiplier=1.2)'),
    ]
    return {Understanding: {'summary': PROMPT, 'relevant_columns': columns, 'assumptions': proposal['assumptions'], 'limitations': limitations, 'exploration_queries': ['SELECT count(*) AS rows, count(DISTINCT customer_id) AS customers FROM raw.source']}, ProposalSet: {'proposals': [proposal]}, BuildResult: {'files': [{'path': 'generated/solution.py', 'content': source}], 'entrypoint': 'generated/solution.py', 'required_dependencies': dependencies, 'expected_outputs': outputs, 'validation_checks': [{'name': name, 'kind': 'sql_assertion', 'target': 'results.customer_review', 'sql': sql} for name, sql in assertions], 'notes': limitations}}, source

class ReviewedShowcaseAdapter:
    """Only this explicit demonstration CLI uses authored contracts."""
    def __init__(self, responses):
        self.responses = responses
    def health(self):
        return {'available': True, 'model_available': True, 'codex_assisted_showcase': True, 'local_llm_used': False}
    def structured(self, schema_class, system, prompt):
        return schema_class.model_validate(self.responses[schema_class])

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approve-showcase-run', action='store_true', help='Approve this reviewed synthetic showcase and execute in real Docker.')
    args = parser.parse_args()
    responses, source = contract()
    settings = replace(Settings.from_env(), max_repair_attempts=0)
    service = Service(settings)
    workspace = service.create_workspace('CODEX-ASSISTED SHOWCASE · Transaction Investigation', PROMPT, (ROOT / 'examples/data/banking_transactions.csv').read_bytes(), 'banking_transactions.csv')
    provenance = {'mode': 'CODEX-ASSISTED SHOWCASE', 'analysis_author': 'Codex assistant', 'local_llm_used': False, 'cloud_api_called_by_application': False, 'synthetic_data': True, 'annotation_file_used': False, 'source_sha256': hashlib.sha256(source.encode()).hexdigest(), 'source_review': 'Compile-only syntax check; explicit source contract and approval. Analysis executes only in Docker.', 'prompt': PROMPT}
    service.manager.write(workspace, 'metadata/showcase.json', provenance)
    workflow = Workflow(settings, llm=ReviewedShowcaseAdapter(responses))
    state = workflow.propose(workspace)
    if args.approve_showcase_run and state.get('status') == 'AWAITING_APPROVAL':
        state = workflow.approve(workspace, 'codex_assisted_showcase', 'Explicit approval of reviewed Codex-assisted analysis on bundled synthetic data; no local LLM was used.')
    print(json.dumps({'workspace_id': workspace.name, 'workspace': str(workspace), 'status': state.get('status'), 'error': state.get('error'), 'local_llm_used': False}, indent=2))
    expected = 'COMPLETED' if args.approve_showcase_run else 'AWAITING_APPROVAL'
    return 0 if state.get('status') == expected else 1

if __name__ == '__main__':
    raise SystemExit(main())
