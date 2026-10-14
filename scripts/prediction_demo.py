"""Run the reviewed synthetic FCCM prediction example in the real sandbox.

This explicit demonstration uses assistant-authored code, not LLM generation.
The production upload/propose workflow still uses the configured model.
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

FEATURES = [
    'amount_log', 'amount_to_median', 'transfers_24h', 'unique_recipients_7d',
    'cross_border_ratio_30d', 'new_recipient_ratio_7d', 'night_transfer_ratio_7d',
    'account_age_days', 'prior_alerts_180d', 'inflow_outflow_ratio_7d',
]
PROMPT = (
    'Build an FCCM-style supervised model to prioritize synthetic transaction-monitoring '
    'alerts for analyst review. Predict the simulated confirmed_escalation outcome using '
    'only the ten pre-alert behavioral features. Respect chronological train, validation '
    'and test windows with a 30-day label-maturity gap. Compare learned models and simple '
    'baselines, choose the model and review threshold on validation only, and evaluate '
    'once on held-out alerts. Show precision-recall, analyst-capacity tradeoffs, false '
    'positives and false negatives, feature importance and five new-alert predictions. '
    'Persist and reload the trained model in Docker, with queryable prediction evidence, '
    'a model card and a reproducible notebook. Synthetic performance does not establish '
    'financial-crime detection quality or justify autonomous decisions about people.'
)


def contract():
    source = (ROOT / 'examples/prediction/solution.py').read_text(encoding='utf-8')
    compile(source, 'generated/solution.py', 'exec', dont_inherit=True)
    required = FEATURES + ['alert_id', 'customer_id', 'alert_time', 'label_available_at', 'confirmed_escalation']
    outputs = []
    for name in ('test_predictions', 'model_comparison', 'threshold_tradeoff',
                 'feature_importance', 'example_predictions', 'split_audit'):
        outputs.extend([
            {'name': name.replace('_', ' ').title(), 'type': 'database_table',
             'location': f'results.{name}', 'min_rows': 1},
            {'name': name.replace('_', ' ').title() + ' CSV', 'type': 'csv',
             'location': f'results/{name}.csv', 'min_rows': 1},
        ])
    for name in ('precision_recall', 'capacity_lift', 'feature_importance', 'score_distribution'):
        outputs.append({'name': name.replace('_', ' ').title(), 'type': 'chart',
                        'location': f'results/{name}_chart.json'})
    outputs.extend([
        {'name': 'Trained Alert Prioritizer', 'type': 'model', 'location': 'models/alert_prioritizer.joblib'},
        {'name': 'Prediction Metrics', 'type': 'json', 'location': 'results/metrics.json'},
        {'name': 'Prediction Dashboard', 'type': 'json', 'location': 'results/dashboard.json'},
        {'name': 'Model Card', 'type': 'text_report', 'location': 'results/model_card.md'},
    ])
    limitations = [
        'CODEX-ASSISTED PREDICTION: assistant-authored source, no local-model generation.',
        'Synthetic labels simulate analyst escalation, not verified crime or actual customer behavior.',
        'No real FCCM product integration or production effectiveness is established.',
        'Model scores are not established calibrated probabilities of financial crime.',
        'New examples have unknown outcomes and are presented for human review only.',
    ]
    proposal = {
        'proposal_id': 'codex_assisted_prediction',
        'title': 'CODEX-ASSISTED PREDICTION · FCCM Alert Prioritization',
        'description': 'Real supervised training and untouched-test evaluation in the restricted Docker sandbox.',
        'approach': PROMPT,
        'implementation_steps': [
            'Validate input, feature allowlist and label availability',
            'Split chronologically, excluding label-maturity gaps',
            'Fit preprocessing and candidate models on training data only',
            'Select by validation average precision and freeze the review threshold',
            'Measure test ranking, capacity and classification metrics without tuning',
            'Save and reload the trained model and score five new examples',
            'Persist model card, queryable evidence and interactive charts',
        ],
        'required_columns': required, 'expected_outputs': outputs,
        'advantages': ['Real trained and reusable model', 'Explicit held-out evaluation and error inspection'],
        'limitations': limitations,
        'assumptions': ['Every row describes a distinct synthetic customer at alert time',
                        'Behavioral features contain only pre-alert history',
                        'Synthetic labels mature 30 days after the alert'],
        'resource_estimate': {'cpu': 'moderate', 'memory': 'under 1 GB expected', 'execution_time': 'under two minutes expected'},
        'validation_strategy': ['Temporal split and label-maturity assertions',
                                'Feature allowlist excludes outcomes and identifiers',
                                'Model reload reproduces original predictions',
                                'Test prediction IDs and labels reconcile to the raw input',
                                'Output artifacts exist and parse'],
        'feasibility': 'FEASIBLE_WITH_LIMITATIONS',
        'confidence_explanation': 'Success requires actual training, persisted results and sandbox validation; no metric target is assumed.',
        'required_dependencies': ['duckdb', 'numpy', 'pandas', 'scikit-learn', 'plotly', 'joblib'],
    }
    assertions = [
        ('Input has 8000 independent alerts', 'SELECT count(*)=8000 AND count(DISTINCT alert_id)=8000 AND count(DISTINCT customer_id)=8000 FROM raw.source'),
        ('Held-out prediction coverage', 'SELECT count(*)=1000 AND count(DISTINCT alert_id)=1000 FROM results.test_predictions'),
        ('Prediction IDs belong only to held-out dates', "SELECT NOT EXISTS (SELECT 1 FROM results.test_predictions p LEFT JOIN raw.source r USING(alert_id) WHERE r.alert_id IS NULL OR CAST(r.alert_time AS TIMESTAMP)<TIMESTAMP '2025-12-17' OR CAST(r.alert_time AS TIMESTAMP)>=TIMESTAMP '2026-02-05')"),
        ('Five new examples scored', 'SELECT count(*)=5 FROM results.example_predictions'),
        ('Ten feature importances saved', 'SELECT count(*)=10 FROM results.feature_importance'),
        ('Scores and frozen decisions are consistent', "SELECT count(*)=1000 AND bool_and(isfinite(escalation_score) AND escalation_score BETWEEN 0 AND 1 AND isfinite(frozen_threshold) AND frozen_threshold BETWEEN 0 AND 1 AND review_recommended=(escalation_score>=frozen_threshold) AND split='test') AND count(DISTINCT frozen_threshold)=1 AND count(DISTINCT model_id)=1 FROM results.test_predictions"),
        ('Held-out truth reconciles to input', 'SELECT count(*)=1000 AND bool_and(p.confirmed_escalation=r.confirmed_escalation AND p.customer_id=r.customer_id AND p.alert_time=CAST(r.alert_time AS TIMESTAMPTZ) AND p.label_available_at=CAST(r.label_available_at AS TIMESTAMPTZ)) FROM results.test_predictions p JOIN raw.source r USING(alert_id)'),
        ('Selected model and threshold match scored predictions', 'SELECT count(*)=1 AND bool_and(m.model=p.model_id AND m.frozen_validation_threshold=p.frozen_threshold) FROM results.model_comparison m CROSS JOIN (SELECT DISTINCT model_id,frozen_threshold FROM results.test_predictions) p WHERE selected_by_validation'),
        ('Selection uses best candidate validation score', "SELECT count(*)=1 AND bool_and(validation_average_precision=(SELECT max(validation_average_precision) FROM results.model_comparison WHERE model IN ('Prevalence dummy','Logistic regression','Random forest'))) FROM results.model_comparison WHERE selected_by_validation"),
        ('Temporal split and label maturity', "SELECT (SELECT rows=4600 FROM results.split_audit WHERE split='train') AND (SELECT rows=1200 FROM results.split_audit WHERE split='validation') AND (SELECT rows=1000 FROM results.split_audit WHERE split='test') AND (SELECT CAST(latest_label_available AS TIMESTAMPTZ) FROM results.split_audit WHERE split='train') < (SELECT CAST(first_alert AS TIMESTAMPTZ) FROM results.split_audit WHERE split='validation') AND (SELECT CAST(latest_label_available AS TIMESTAMPTZ) FROM results.split_audit WHERE split='validation') < (SELECT CAST(first_alert AS TIMESTAMPTZ) FROM results.split_audit WHERE split='test') AND (SELECT max(label_available_at) FROM results.test_predictions)<TIMESTAMPTZ '2026-03-07 00:00:00+00'"),
        ('Average precision independently recomputed from scored rows', 'WITH score_groups AS (SELECT escalation_score,count(*) AS n,sum(confirmed_escalation) AS pos FROM results.test_predictions GROUP BY escalation_score), cumulative AS (SELECT pos,sum(pos) OVER(ORDER BY escalation_score DESC ROWS UNBOUNDED PRECEDING) AS tp,sum(n) OVER(ORDER BY escalation_score DESC ROWS UNBOUNDED PRECEDING) AS reviewed,sum(pos) OVER() AS positives FROM score_groups), recomputed AS (SELECT sum((pos::DOUBLE/positives)*(tp::DOUBLE/reviewed)) AS ap FROM cumulative) SELECT abs(recomputed.ap-m.test_average_precision)<1e-12 FROM recomputed CROSS JOIN results.model_comparison m WHERE m.selected_by_validation'),
        ('Brier score independently recomputed from scored rows', 'SELECT abs((SELECT avg(power(escalation_score-confirmed_escalation,2)) FROM results.test_predictions)-test_brier_score)<1e-12 FROM results.model_comparison WHERE selected_by_validation'),
    ]
    return {
        Understanding: {'summary': PROMPT, 'relevant_columns': required,
                        'assumptions': proposal['assumptions'], 'limitations': limitations,
                        'exploration_queries': ['SELECT count(*) AS rows, avg(confirmed_escalation) AS synthetic_prevalence FROM raw.source']},
        ProposalSet: {'proposals': [proposal]},
        BuildResult: {'files': [{'path': 'generated/solution.py', 'content': source}],
                      'entrypoint': 'generated/solution.py',
                      'required_dependencies': proposal['required_dependencies'],
                      'expected_outputs': outputs,
                      'validation_checks': [{'name': name, 'kind': 'sql_assertion', 'target': 'results.test_predictions', 'sql': sql} for name, sql in assertions],
                      'notes': limitations},
    }, source


class ReviewedPredictionAdapter:
    def __init__(self, responses):
        self.responses = responses

    def health(self):
        return {'available': True, 'model_available': True, 'assistant_authored': True, 'local_llm_used': False}

    def structured(self, schema_class, system, prompt):
        return schema_class.model_validate(self.responses[schema_class])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approve-prediction-run', action='store_true')
    args = parser.parse_args()
    responses, source = contract()
    settings = replace(Settings.from_env(), max_repair_attempts=0)
    service = Service(settings)
    dataset = ROOT / 'examples/prediction/synthetic_alerts.csv'
    workspace = service.create_workspace('CODEX-ASSISTED PREDICTION · FCCM Alert Prioritization', PROMPT, dataset.read_bytes(), dataset.name)
    service.manager.write(workspace, 'metadata/prediction.json', {
        'mode': 'CODEX-ASSISTED PREDICTION', 'analysis_author': 'Codex assistant',
        'local_llm_used': False, 'cloud_api_called_by_application': False,
        'synthetic_data': True, 'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
        'dataset_sha256': hashlib.sha256(dataset.read_bytes()).hexdigest(),
        'prompt': PROMPT,
    })
    workflow = Workflow(settings, llm=ReviewedPredictionAdapter(responses))
    state = workflow.propose(workspace)
    if args.approve_prediction_run and state.get('status') == 'AWAITING_APPROVAL':
        state = workflow.approve(workspace, 'codex_assisted_prediction',
                                 'User requested a synthetic FCCM prediction demonstration. Approved reviewed source; real model training in Docker, no LLM generation.')
    print(json.dumps({'workspace_id': workspace.name, 'workspace': str(workspace),
                      'status': state.get('status'), 'error': state.get('error'), 'local_llm_used': False}, indent=2))
    return 0 if state.get('status') == ('COMPLETED' if args.approve_prediction_run else 'AWAITING_APPROVAL') else 1


if __name__ == '__main__':
    raise SystemExit(main())
