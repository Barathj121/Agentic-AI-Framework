"""Reviewed synthetic alert-prioritization experiment. Execute only in Docker.

This trains actual estimators, freezes selection and a threshold on validation,
then evaluates untouched chronological test rows. It is not a crime detector.
"""
from pathlib import Path
import hashlib
import json
import math
import duckdb
import joblib
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, confusion_matrix, f1_score, precision_recall_curve, precision_score, recall_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

assert Path('/.dockerenv').exists(), 'Execute the predictive experiment only in the approved Docker sandbox'
FEATURES = ['amount_log', 'amount_to_median', 'transfers_24h', 'unique_recipients_7d', 'cross_border_ratio_30d', 'new_recipient_ratio_7d', 'night_transfer_ratio_7d', 'account_age_days', 'prior_alerts_180d', 'inflow_outflow_ratio_7d']
TARGET = 'confirmed_escalation'
SEED = 20261015
ANCHOR = pd.Timestamp('2025-01-01', tz='UTC')
REVIEW_FRACTION = .10
con = duckdb.connect('database/workspace.duckdb')
data = con.execute('SELECT * FROM raw.source ORDER BY alert_time, alert_id').df()
assert len(data) == 8000, 'This reviewed experiment expects the bundled 8,000-row synthetic alert dataset'
assert data.alert_id.is_unique and data.customer_id.is_unique, 'Each row must be an independent alert and unique synthetic customer'
data['alert_time'] = pd.to_datetime(data.alert_time, utc=True)
data['label_available_at'] = pd.to_datetime(data.label_available_at, utc=True)
data[TARGET] = pd.to_numeric(data[TARGET]).astype(int)
assert set(data[TARGET].unique()) == {0, 1}
assert (data.label_available_at >= data.alert_time + pd.Timedelta(days=30)).all(), 'Outcome labels need at least 30 days to mature'
for column in FEATURES:
    data[column] = pd.to_numeric(data[column], errors='raise')
assert not np.isinf(data[FEATURES].to_numpy(dtype=float)).any()
assert set(FEATURES).isdisjoint({TARGET, 'alert_id', 'customer_id', 'alert_time', 'label_available_at'})
data['day_offset'] = ((data.alert_time - ANCHOR) / pd.Timedelta(days=1)).astype(int)
definitions = [('train', 0, 230, 260), ('validation', 260, 320, 350), ('test', 350, 400, 430)]
splits, audit = {}, []
for name, start, stop, available_day in definitions:
    time_mask = data.day_offset.ge(start) & data.day_offset.lt(stop)
    maturity_cutoff = ANCHOR + pd.Timedelta(days=available_day)
    selected = data[time_mask & data.label_available_at.le(maturity_cutoff)].copy()
    assert len(selected) > 0 and selected[TARGET].nunique() == 2
    splits[name] = selected
    audit.append({'split': name, 'rows': len(selected), 'start_day_inclusive': start, 'end_day_exclusive': stop, 'labels_known_by_day': available_day, 'first_alert': str(selected.alert_time.min()), 'last_alert': str(selected.alert_time.max()), 'latest_label_available': str(selected.label_available_at.max()), 'positive_labels': int(selected[TARGET].sum()), 'positive_fraction': float(selected[TARGET].mean()), 'immature_rows_excluded': int(time_mask.sum() - len(selected))})
train, validation, test = (splits[name] for name in ['train', 'validation', 'test'])
assert [len(train), len(validation), len(test)] == [4600, 1200, 1000]
assert train.label_available_at.max() < validation.alert_time.min()
assert validation.label_available_at.max() < test.alert_time.min()
used_ids = set(train.alert_id) | set(validation.alert_id) | set(test.alert_id)
assert len(used_ids) == sum(len(frame) for frame in splits.values())
excluded_rows = len(data) - len(used_ids)
assert excluded_rows == 1200
X_train, y_train = train[FEATURES], train[TARGET]
X_val, y_val = validation[FEATURES], validation[TARGET]
X_test, y_test = test[FEATURES], test[TARGET]

candidates = {
    'Prevalence dummy': Pipeline([('impute', SimpleImputer(strategy='median')), ('model', DummyClassifier(strategy='prior'))]),
    'Logistic regression': Pipeline([('impute', SimpleImputer(strategy='median')), ('scale', StandardScaler()), ('model', LogisticRegression(C=1, max_iter=1200, random_state=SEED))]),
    'Random forest': Pipeline([('impute', SimpleImputer(strategy='median')), ('model', RandomForestClassifier(n_estimators=200, max_depth=9, min_samples_leaf=12, max_features=.8, n_jobs=1, random_state=SEED))]),
}
val_scores, validation_ap = {}, {}
for name, estimator in candidates.items():
    estimator.fit(X_train, y_train)
    val_scores[name] = estimator.predict_proba(X_val)[:, 1]
    validation_ap[name] = float(average_precision_score(y_val, val_scores[name]))
# Freeze model selection and the operational threshold BEFORE reading test labels
# into any evaluation call. No model is refit on validation or test data.
selected_name = max(validation_ap, key=validation_ap.get)
selected_model = candidates[selected_name]
frozen_threshold = float(np.quantile(val_scores[selected_name], 1 - REVIEW_FRACTION))
bundle = {'model': selected_model, 'features': FEATURES, 'target': TARGET, 'selected_model': selected_name, 'threshold': frozen_threshold, 'threshold_rule': '90th percentile of validation scores; intended validation review capacity 10%, with ties retained', 'selection_rule': 'Maximum validation average precision only', 'validation_average_precision': validation_ap[selected_name], 'random_seed': SEED, 'training_rows': len(train), 'synthetic_only': True, 'score_interpretation': 'Uncalibrated estimate of a simulated analyst escalation label; not a crime probability'}
Path('models').mkdir(exist_ok=True)
joblib.dump(bundle, 'models/alert_prioritizer.joblib')
reloaded = joblib.load('models/alert_prioritizer.joblib')
predicted_before_reload = selected_model.predict_proba(X_test)[:, 1]
test_scores_selected = reloaded['model'].predict_proba(X_test[reloaded['features']])[:, 1]
np.testing.assert_array_equal(predicted_before_reload, test_scores_selected)
assert reloaded['threshold'] == frozen_threshold

def evaluate(labels, scores, threshold):
    decisions = np.asarray(scores) >= threshold
    tn, fp, fn, tp = confusion_matrix(labels, decisions, labels=[0, 1]).ravel()
    precision = float(precision_score(labels, decisions, zero_division=0))
    prevalence = float(np.mean(labels))
    return {'average_precision': float(average_precision_score(labels, scores)), 'roc_auc': float(roc_auc_score(labels, scores)), 'precision': precision, 'recall': float(recall_score(labels, decisions, zero_division=0)), 'f1': float(f1_score(labels, decisions, zero_division=0)), 'brier_score': float(brier_score_loss(labels, scores)), 'review_count': int(decisions.sum()), 'review_fraction': float(decisions.mean()), 'lift_over_prevalence': precision / prevalence if prevalence else 0., 'true_positive': int(tp), 'false_positive': int(fp), 'false_negative': int(fn), 'true_negative': int(tn)}

def top_capacity(labels, scores, fraction):
    count = max(1, int(math.ceil(len(scores) * fraction)))
    # Stable ties resolve by the existing chronological alert_id order.
    indexes = np.argsort(-np.asarray(scores), kind='stable')[:count]
    positives = int(np.asarray(labels)[indexes].sum())
    precision = positives / count
    prevalence = float(np.mean(labels))
    return {'review_count': count, 'true_positive': positives, 'precision': precision, 'recall': positives / int(np.sum(labels)), 'lift': precision / prevalence}

# This fixed formula is a separately labelled heuristic comparator, never the
# learned model or the outcome-generating mechanism.
def heuristic(frame):
    return np.log1p(frame.amount_to_median.clip(lower=0).to_numpy()) + np.log1p(frame.transfers_24h.clip(lower=0).to_numpy())

test_scores = {name: estimator.predict_proba(X_test)[:, 1] for name, estimator in candidates.items()}
test_scores['Fixed amount/activity heuristic'] = heuristic(X_test)
val_scores['Fixed amount/activity heuristic'] = heuristic(X_val)
comparison = []
for name, scores in test_scores.items():
    capacity = top_capacity(y_test, scores, REVIEW_FRACTION)
    threshold = float(np.quantile(val_scores[name], .9))
    decisions = scores >= threshold
    comparison.append({'model': name, 'selected_by_validation': name == selected_name, 'validation_average_precision': float(average_precision_score(y_val, val_scores[name])), 'test_average_precision': float(average_precision_score(y_test, scores)), 'test_roc_auc': float(roc_auc_score(y_test, scores)), 'test_precision_at_10pct_capacity': capacity['precision'], 'test_recall_at_10pct_capacity': capacity['recall'], 'test_lift_at_10pct_capacity': capacity['lift'], 'frozen_validation_threshold': threshold, 'test_frozen_threshold_review_count': int(decisions.sum()), 'test_frozen_threshold_precision': float(precision_score(y_test, decisions, zero_division=0)), 'test_frozen_threshold_recall': float(recall_score(y_test, decisions, zero_division=0)), 'test_brier_score': float(brier_score_loss(y_test, scores)) if name in candidates else None})
comparison = pd.DataFrame(comparison)
heldout = evaluate(y_test, test_scores_selected, frozen_threshold)
capacity_10 = top_capacity(y_test, test_scores_selected, REVIEW_FRACTION)
baseline_10 = top_capacity(y_test, test_scores['Fixed amount/activity heuristic'], REVIEW_FRACTION)
predictions = test[['alert_id', 'customer_id', 'alert_time', 'label_available_at', TARGET, *FEATURES]].copy()
predictions['escalation_score'] = test_scores_selected
predictions['frozen_threshold'] = frozen_threshold
predictions['review_recommended'] = predictions.escalation_score.ge(frozen_threshold)
predictions['queue_action'] = np.where(predictions.review_recommended, 'PRIORITIZE FOR ANALYST REVIEW', 'BELOW THIS QUEUE THRESHOLD')
predictions['model_id'] = selected_name
predictions['split'] = 'test'
predictions = predictions.sort_values(['escalation_score', 'alert_id'], ascending=[False, True])
predictions.insert(0, 'review_rank', range(1, len(predictions) + 1))
tradeoffs = []
for capacity in [.02, .05, .10, .15, .20, .30, .50]:
    cutoff = float(np.quantile(val_scores[selected_name], 1-capacity))
    evaluated = evaluate(y_test, test_scores_selected, cutoff)
    top = top_capacity(y_test, test_scores_selected, capacity)
    baseline = top_capacity(y_test, test_scores['Fixed amount/activity heuristic'], capacity)
    tradeoffs.append({'target_validation_review_fraction': capacity, 'threshold_from_validation': cutoff, 'chosen_operating_point': capacity == REVIEW_FRACTION, 'test_actual_review_count': evaluated['review_count'], 'test_actual_review_fraction': evaluated['review_fraction'], 'test_precision': evaluated['precision'], 'test_recall': evaluated['recall'], 'test_f1': evaluated['f1'], 'test_top_capacity_review_count': top['review_count'], 'test_top_capacity_precision': top['precision'], 'test_top_capacity_recall': top['recall'], 'test_top_capacity_lift': top['lift'], 'heuristic_top_capacity_precision': baseline['precision'], 'heuristic_top_capacity_recall': baseline['recall'], 'heuristic_top_capacity_lift': baseline['lift']})
tradeoffs = pd.DataFrame(tradeoffs)
# Test-label permutation is post-hoc interpretation of the frozen model, not
# feature selection, parameter tuning or an input to the fitted estimator.
permutation = permutation_importance(reloaded['model'], X_test, y_test, scoring='average_precision', n_repeats=5, random_state=SEED, n_jobs=1)
importance = pd.DataFrame({'feature': FEATURES, 'average_precision_drop_mean': permutation.importances_mean, 'average_precision_drop_std': permutation.importances_std}).sort_values('average_precision_drop_mean', ascending=False)

fresh = pd.DataFrame([
    [7.0, .8, 1, 2, .02, .05, .03, 2100, 0, .8],
    [9.2, 9., 18, 12, .55, .8, .65, 45, 3, 2.8],
    [10.2, 12., 2, 2, .05, .1, .02, 1800, 0, .95],
    [7.8, 2.2, 24, 18, .7, .85, .55, 75, 2, 3.2],
    [8.3, 1.7, 5, 5, .1, .3, .15, 800, 1, 1.1],
], columns=FEATURES)
fresh_scores = reloaded['model'].predict_proba(fresh[reloaded['features']])[:, 1]
examples = fresh.copy()
examples.insert(0, 'example_id', ['NEW-001', 'NEW-002', 'NEW-003', 'NEW-004', 'NEW-005'])
examples['escalation_score'] = fresh_scores
examples['frozen_threshold'] = frozen_threshold
examples['review_recommended'] = examples.escalation_score.ge(frozen_threshold)
examples['outcome'] = 'UNKNOWN: independently constructed example, no outcome label'
examples['scored_by'] = selected_name + ' (reloaded saved model)'
split_audit = pd.DataFrame(audit)
tables = {'test_predictions': predictions, 'model_comparison': comparison, 'threshold_tradeoff': tradeoffs, 'feature_importance': importance, 'example_predictions': examples, 'split_audit': split_audit}
for name, frame in tables.items():
    con.register('computed_frame', frame)
    con.execute(f'CREATE OR REPLACE TABLE results.{name} AS SELECT * FROM computed_frame')
    frame.to_csv(f'results/{name}.csv', index=False)
    con.unregister('computed_frame')

colors = {'Prevalence dummy': '#94a3b8', 'Logistic regression': '#6979f8', 'Random forest': '#00ccb4', 'Fixed amount/activity heuristic': '#ffb65c'}
def save_chart(fig, name):
    fig.update_layout(template='plotly_dark', paper_bgcolor='#101a2c', plot_bgcolor='#101a2c', font={'family': 'Arial', 'size': 13, 'color': '#f2f5fa'}, title_font={'color': '#f2f5fa', 'size': 17}, margin={'l': 55, 'r': 30, 't': 75, 'b': 60}, height=490, legend={'orientation': 'h', 'y': -0.25, 'font': {'color': '#f2f5fa', 'size': 11}})
    fig.update_xaxes(tickfont={'color': '#f2f5fa'}, title_font={'color': '#f2f5fa'}, gridcolor='#26364e')
    fig.update_yaxes(tickfont={'color': '#f2f5fa'}, title_font={'color': '#f2f5fa'}, gridcolor='#26364e')
    fig.update_annotations(font_color='#f2f5fa')
    Path('results/' + name + '.json').write_text(fig.to_json(), encoding='utf-8')

fig = go.Figure()
for name, scores in test_scores.items():
    p, r, _ = precision_recall_curve(y_test, scores)
    ap = average_precision_score(y_test, scores)
    fig.add_scatter(x=r.tolist(), y=p.tolist(), name=f'{name} (AP {ap:.3f})', mode='lines', line={'color': colors[name], 'width': 3 if name == selected_name else 1.5})
fig.add_hline(y=float(y_test.mean()), line_dash='dot', line_color='#94a3b8')
fig.update_layout(title='Untouched test set: precision vs. recall', xaxis_title='Recall of simulated escalations', yaxis_title='Precision among reviewed alerts', yaxis_range=[0, 1.05], xaxis_range=[0, 1])
save_chart(fig, 'precision_recall_chart')
fig = go.Figure()
fig.add_scatter(x=(tradeoffs.target_validation_review_fraction * 100).tolist(), y=tradeoffs.test_top_capacity_recall.tolist(), name=selected_name, mode='lines+markers', line={'color': '#00ccb4', 'width': 3})
fig.add_scatter(x=(tradeoffs.target_validation_review_fraction * 100).tolist(), y=tradeoffs.heuristic_top_capacity_recall.tolist(), name='Fixed heuristic', mode='lines+markers', line={'color': '#ffb65c'})
fig.add_scatter(x=(tradeoffs.target_validation_review_fraction * 100).tolist(), y=tradeoffs.target_validation_review_fraction.tolist(), name='Random review expectation', mode='lines', line={'dash': 'dot', 'color': '#94a3b8'})
fig.update_layout(title='Same analyst capacity: how much is captured?', xaxis_title='Test alerts reviewed (%)', yaxis_title='Recall at exact ranked capacity', yaxis_range=[0, 1])
save_chart(fig, 'capacity_lift_chart')
ordered = importance.sort_values('average_precision_drop_mean')
fig = go.Figure(go.Bar(y=ordered.feature.tolist(), x=ordered.average_precision_drop_mean.tolist(), orientation='h', error_x={'type': 'data', 'array': ordered.average_precision_drop_std.tolist()}, marker_color=['#00ccb4' if value >= 0 else '#ffb65c' for value in ordered.average_precision_drop_mean]))
fig.update_layout(title='Which features help the frozen model?', xaxis_title='Test AP decrease after shuffling (5 repeats)', yaxis={'automargin': True})
save_chart(fig, 'feature_importance_chart')
fig = go.Figure()
for label, color, title in [(0, '#6979f8', 'No simulated escalation'), (1, '#00ccb4', 'Simulated escalation')]:
    fig.add_histogram(x=test_scores_selected[y_test.to_numpy() == label].tolist(), name=title, opacity=.7, xbins={'start': 0, 'end': 1, 'size': .04}, marker_color=color)
fig.add_vline(x=frozen_threshold, line_dash='dash', line_color='#ffb65c', annotation_text='Frozen validation threshold')
fig.update_layout(title='Held-out scores and the review threshold', xaxis_title='Model score (not calibrated crime probability)', yaxis_title='Test alerts', barmode='overlay')
save_chart(fig, 'score_distribution_chart')

limitations = [
    'Labels are simulated analyst-escalation outcomes from a synthetic data generator; they are not evidence of crime, laundering or regulatory violations.',
    'The experiment evaluates synthetic temporal generalization only. It has not been validated against real FCCM alerts or investigated outcomes.',
    'Scores estimate the simulated target and have not been calibrated. Brier score is a diagnostic, not a calibration guarantee.',
    'Features must be computed using information available at each alert time. This synthetic dataset supplies those features directly; production aggregation requires separate point-in-time controls.',
    'The frozen threshold targets 10% review on validation. Future and test review rates can differ because score distributions drift or ties occur.',
    'The fixed-capacity comparison ranks exactly 10% of test alerts, separately from deployment at the frozen threshold. Test curves are descriptive and did not select the model or threshold.',
    'One unique synthetic customer per row avoids customer overlap. Real repeated-customer data needs customer-aware temporal evaluation.',
    'Feature permutation is global post-hoc association on the test set, not a causal explanation or an explanation of a particular alert.',
    'The five fresh cases have unknown outcomes. Their predicted scores demonstrate saved-model inference, not correct predictions.',
    'Hyperparameters were specified before evaluation. The winning candidate was selected only by validation average precision; test outcomes were used for final reporting and interpretation.',
]
provenance = {'mode': 'CODEX-ASSISTED PREDICTIVE EXPERIMENT', 'analysis_author': 'Codex assistant, reviewed source', 'local_llm_used': False, 'synthetic_data': True, 'actual_model_training': True, 'execution': 'Real isolated Docker sandbox', 'synthetic_target': TARGET, 'target_meaning': 'Simulated analyst escalation after investigation; never proof of crime'}
metrics = {'provenance': provenance, 'rows': len(data), 'feature_count': len(FEATURES), 'features': FEATURES, 'excluded_feature_columns': ['alert_id', 'customer_id', 'alert_time', 'label_available_at', TARGET, 'day_offset'], 'split_audit': audit, 'excluded_gap_rows': excluded_rows, 'selection': {'model': selected_name, 'criterion': 'Validation average precision only', 'validation_average_precision_by_candidate': validation_ap, 'refit_on_validation': False, 'test_used_for_selection': False}, 'threshold': {'value': frozen_threshold, 'source': 'Validation scores only, 90th percentile', 'target_review_fraction': REVIEW_FRACTION, 'actual_validation_review_fraction': float((val_scores[selected_name] >= frozen_threshold).mean()), 'frozen_before_test_evaluation': True}, 'heldout_test': {'rows': len(test), 'positive_labels': int(y_test.sum()), 'prevalence': float(y_test.mean()), **heldout}, 'fixed_10pct_test_capacity': {'selected_model': capacity_10, 'fixed_heuristic': baseline_10, 'interpretation': 'Same count of highest-ranked test alerts; separate from the frozen-threshold deployment evaluation'}, 'saved_model_reload': {'identical_test_predictions': True, 'comparison': 'numpy.assert_array_equal on all 1000 test scores', 'fresh_cases_scored': len(examples), 'fresh_case_labels_known': False}, 'model_artifact': 'models/alert_prioritizer.joblib', 'limitations': limitations}
Path('results/metrics.json').write_text(json.dumps(metrics, indent=2, allow_nan=False), encoding='utf-8')
findings = [
    f'{selected_name} was chosen by validation average precision ({validation_ap[selected_name]:.3f}); the {len(test):,} later test alerts were excluded from fitting and selection.',
    f'Test average precision is {heldout["average_precision"]:.3f} against a prevalence baseline of {y_test.mean():.3f}; ROC-AUC is {heldout["roc_auc"]:.3f}. These are synthetic held-out outcomes.',
    f'At the frozen validation threshold {frozen_threshold:.3f}, {heldout["review_count"]} test alerts enter the queue: {heldout["true_positive"]} simulated escalations and {heldout["false_positive"]} false positives; {heldout["false_negative"]} simulated escalations remain outside it.',
    f'At exactly 10% test review capacity ({capacity_10["review_count"]} alerts), the learned model captures {capacity_10["true_positive"]} simulated escalations versus {baseline_10["true_positive"]} for the fixed amount/activity heuristic.',
    'The saved estimator was reloaded inside Docker and reproduced all 1,000 test scores exactly; it also scored five new constructed cases whose outcomes are unknown.',
]
dashboard = {'schema': 'workbench.dashboard.v1', 'title': 'Predictive Alert Prioritization', 'summary': 'Actual supervised learning on 8,000 synthetic alerts, with mature labels, chronological holdout, validation-only selection, a frozen review threshold and a reusable saved model.', 'provenance': 'CODEX-ASSISTED PREDICTIVE EXPERIMENT. Real trained estimators executed in Docker. Synthetic simulated escalation labels; no real-world FCCM accuracy or crime-probability claim. No local LLM inference.', 'metrics': [{'label': 'Held-out average precision', 'value': f'{heldout["average_precision"]:.3f}', 'help': f'Average precision on {len(test)} untouched later alerts; prevalence baseline {y_test.mean():.3f}. Non-interpolated average precision, distinct from trapezoidal PR-AUC.'}, {'label': 'Recall at frozen threshold', 'value': f'{heldout["recall"]:.1%}', 'help': 'Fraction of held-out simulated escalations prioritized at the validation-only threshold.'}, {'label': 'Precision at frozen threshold', 'value': f'{heldout["precision"]:.1%}', 'help': 'Fraction of the selected held-out review queue carrying the simulated escalation label.'}, {'label': 'Test alerts prioritized', 'value': f'{heldout["review_count"]} / {len(test):,}', 'help': 'The threshold targets 10% on validation; actual later review volume can differ.'}], 'findings': findings, 'charts': ['results/precision_recall_chart.json', 'results/capacity_lift_chart.json', 'results/feature_importance_chart.json', 'results/score_distribution_chart.json']}
Path('results/dashboard.json').write_text(json.dumps(dashboard, indent=2), encoding='utf-8')
card = '# Synthetic alert prioritization model card\n\n'
card += 'CODEX-ASSISTED PREDICTIVE EXPERIMENT. This is an actual fitted supervised model, with assistant-authored reviewed training code and real Docker execution. No local LLM was called.\n\n'
card += '## Intended use and target\n\nResearch demonstration of analyst queue prioritization using simulated post-investigation escalation labels. This model must not be used to accuse people of crime or make real operational decisions without independent validation.\n\n'
card += '## Features and leakage controls\n\nTen numeric features only: ' + ', '.join(FEATURES) + '. IDs, timestamps, label availability and the outcome are excluded. Imputation and scaling are fitted on training rows only. Each alert has a distinct synthetic customer.\n\n'
card += '## Chronological split and label maturity\n\nDays are measured from 2025-01-01 UTC. Train days [0,230): 4,600 alerts, labels available before day 260. Validation [260,320): 1,200 alerts, labels available before day 350. Test [350,400): 1,000 alerts, labels observed by day 430. The two 30-day gaps exclude 1,200 rows.\n\n'
card += '## Selection and threshold\n\nPrevalence dummy, logistic regression and random forest were trained on the same training rows. Selected ' + selected_name + ' using validation average precision only. The threshold ' + f'{frozen_threshold:.6f}' + ' is the 90th percentile of validation scores and was frozen before test evaluation. No refit or test-based tuning occurred. The fixed amount/activity score is a separate heuristic comparator.\n\n'
card += '## Held-out results\n\n' + '\n\n'.join(findings) + '\n\n```json\n' + json.dumps(heldout, indent=2) + '\n```\n\n'
card += '## Saved-model verification\n\n`models/alert_prioritizer.joblib` contains the fitted pipeline, exact feature list, frozen threshold and selection metadata. It was reloaded inside Docker; all 1,000 held-out scores matched exactly. Five independently constructed cases were scored with no outcome labels. Do not deserialize the artifact in the host UI.\n\n'
card += '## Limitations\n\n' + '\n'.join('- ' + item for item in limitations)
Path('results/model_card.md').write_text(card, encoding='utf-8')
print(json.dumps({'selected_model': selected_name, 'validation_average_precision': validation_ap, 'heldout_test': metrics['heldout_test'], 'fixed_capacity_comparison': metrics['fixed_10pct_test_capacity'], 'reload_verified': True, 'new_case_scores': examples[['example_id', 'escalation_score', 'review_recommended']].to_dict(orient='records')}, indent=2, allow_nan=False))
con.close()
