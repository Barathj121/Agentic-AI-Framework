# Build and verify a supervised prediction workflow

Use this prompt with a coding agent or a local model connected to the Data Solution Workbench. The checked-in demonstration was authored with Codex assistance; it is not evidence of successful local Ollama code generation. The model's generated code must still pass the same execution and data checks.

## Objective

Build an actual supervised model that predicts `confirmed_escalation`, a simulated analyst escalation outcome, from prior-behavior features in `synthetic_alerts.csv`. Produce a reproducible trained artifact, honest held-out metrics, individual predictions and an interactive summary. All reported numeric results must come from executed code and persisted outputs. Do not invent metrics, substitute hand-written scores for fitted predictions, or describe synthetic escalation likelihood as verified fraud probability.

Run generated Python only inside the approved Docker sandbox. Use the workbench's workspace database and output directories. Persist readable source, logs, validation evidence and a notebook. Keep execution bounded and use installed dependencies; do not download packages or call external services from generated code.

## Stage 1: Inspect and propose

1. Inspect the actual input schema, row count, dtypes, missingness, unique identifiers, time coverage and target values. Assert 8,000 rows, 20 alerts per UTC day, 400 days, unique `alert_id` and unique `customer_id`.
2. Verify that every `label_available_at` is exactly 30 days after its `alert_time`. Check numeric predictors are finite and the target contains only 0 and 1. Record observations without modifying labels to improve scores.
3. Use exactly these ten predictors, in this recorded order:

   ```text
   amount_log
   amount_to_median
   transfers_24h
   unique_recipients_7d
   cross_border_ratio_30d
   new_recipient_ratio_7d
   night_transfer_ratio_7d
   account_age_days
   prior_alerts_180d
   inflow_outflow_ratio_7d
   ```

4. Exclude `alert_id`, `customer_id`, `alert_time`, `label_available_at`, `day_offset` and `confirmed_escalation` from every model's feature matrix. Do not import generator internals, hidden probabilities or outcome noise into training. IDs are allowed only for joins and deterministic tie ordering.
5. Propose a simple baseline and at most two small supervised candidate models, with fixed documented seeds and bounded resource requirements. A train-prevalence baseline, a standardized logistic model and a compact tree ensemble are suitable starting points. Do not use a large search or optimize on the final test.
6. Present the planned splits, validation model-selection criterion, threshold rule, expected artifacts and checks. Await the workbench's explicit approval before training or running generated code.

## Stage 2: Freeze chronological splits and leakage controls

Use elapsed UTC calendar days from January 1, 2025. Cross-check the saved `day_offset` against `alert_time`; do not use it as a predictor.

| Role | Day-offset rule | Rows |
|---|---|---:|
| Training | `0 <= d < 230` | 4,600 |
| Excluded maturity gap | `230 <= d < 260` | 600 |
| Validation | `260 <= d < 320` | 1,200 |
| Excluded maturity gap | `320 <= d < 350` | 600 |
| Final test | `350 <= d < 400` | 1,000 |

Persist split membership and excluded-row counts. Prove that IDs are disjoint across partitions, train labels are available before the first validation alert, and validation labels are available before the first test alert. Record the dataset observation cutoff that makes final test outcomes available. Do not fit, select or tune anything on gap rows or final test labels.

Fit imputers, scalers, encoders and estimators using training rows only. Package preprocessing and the estimator together, preferably as a scikit-learn pipeline. Do not compute standardization, imputation constants, feature selection or target-derived features using all rows.

## Stage 3: Fit and select using validation only

1. Fit the train-prevalence baseline and predeclared candidate models. Record seeds, feature order, parameters, training row count and elapsed time.
2. Compute validation predicted probabilities. Compare candidates using a predeclared primary metric such as average precision, with an explicit deterministic tie rule. Include ROC-AUC and probability-quality metrics as secondary evidence. Distinguish average precision from trapezoidal integration of a precision-recall curve.
3. Select the candidate using validation results only. Keep the selected train-fitted pipeline fixed. Do not refit on validation rows after selecting a threshold unless an additional independent calibration design was approved in advance.
4. Using only the selected model's validation scores, choose a threshold under a maximum 10% review capacity: at most 120 of the 1,200 validation alerts may be selected. Define score comparison (`>=` or `>`), tie treatment and the selection criterion explicitly. If ties prevent exactly 10%, accept fewer rather than exceeding the limit or using test information. A threshold scan that maximizes validation recall under the capacity limit is acceptable.
5. Persist the selected candidate, threshold, comparison rule, validation review count and selection evidence before final test evaluation. Do not search for a better threshold after looking at test results.

## Stage 4: Evaluate the untouched final test

Use the frozen selected pipeline and threshold on all 1,000 final test rows. Report actual computed results, even if they disappoint. Include:

- Final test class prevalence and sample count.
- Selected model and baseline ROC-AUC and average precision.
- Brier score and log loss, with clipping conventions if needed. These measure probability quality; do not claim calibration merely because probabilities exist.
- Confusion matrix, precision, recall and F1 using the frozen validation threshold, with explicit handling for zero denominators.
- Actual test review count and fraction under the frozen threshold. The test workload can differ from validation's 10% cap; report it without retuning.
- Precision, recall and lift among the top 10% of final test scores as a separate ranking diagnostic. Declare stable tie ordering and define lift as precision in that group divided by test prevalence. This diagnostic must not replace the frozen-threshold evaluation.
- Comparable baseline evidence and a clear statement of which model beat which baseline on which metric. Do not claim universal superiority from a single measure.

Persist per-alert test IDs, actual labels, predicted probability, frozen-threshold decision and ranking position. Do not include actual labels when scoring fresh examples. Clearly label any feature-importance method, dataset used and limitations; do not treat importance as causality or as a reason to reselect features after final testing.

## Stage 5: Save, reload and predict

1. Save the fitted preprocessing-plus-estimator artifact, ordered feature list, threshold, model parameters and split metadata. Record an artifact hash and library versions.
2. Reload the saved artifact inside the sandbox. Verify that reloaded probabilities match the in-memory model on a fixed set of rows within a documented numeric tolerance. Fail validation if they disagree.
3. Create five clearly synthetic fresh feature snapshots with plausible values, without target labels. Pass only the exact ten predictors through the reloaded pipeline. Persist their probabilities and review decisions; do not fabricate true outcomes or call these examples a new accuracy test.
4. Include the reusable prediction entry point or function. Enforce the exact feature schema, numeric types and finite values; reject missing or unexpected predictor columns rather than silently scoring an inconsistent array.

## Stage 6: Present real evidence

Create small, readable Plotly JSON charts using only persisted computed values, such as held-out precision-recall/ROC curves, ranking lift, a final test confusion matrix and an honestly labeled explanation plot. Charts must not reference external resources. Provide a model card covering data, target, splits, selection, threshold policy, metrics, intended demonstration use and limitations.

For the workbench Results overview, declare a JSON artifact using this presentation schema:

```json
{
  "schema": "workbench.dashboard.v1",
  "title": "Supervised Alert Prediction",
  "summary": "A short description grounded in the executed run.",
  "metrics": [
    {"label": "Example label", "value": "computed value", "help": "Metric definition and evaluation split"}
  ],
  "findings": ["Computed finding with its scope and limitations."],
  "charts": ["results/registered_chart.json"],
  "provenance": "State who authored the code, whether local LLM generation occurred, and that the data is synthetic."
}
```

This is a format illustration, not fabricated result content. Replace every placeholder with executed evidence. Declare the dashboard and charts as expected outputs so they receive recorded validation; the UI only displays validated registered reports and chart references. Use at most eight metrics, twelve findings and four charts. Keep summary text concise, and let detailed metrics, predictions, model card and notebook remain available as artifacts.

## Acceptance checks

The program must fail its validation if any required check fails:

- Exact dataset/split counts; unique IDs; finite features; valid binary target; exact label maturity; disjoint partitions; mature earlier labels.
- Exact predictor allowlist throughout fitting and scoring; no target, identifier, date, availability field or generator internals as predictors.
- Candidate selection and threshold selection use validation only; preprocessing is fitted on training only; test data is not used for retries or optimization.
- Declared baselines and model metrics are computed, finite where mathematically defined, and use explicitly named splits.
- Confusion-matrix counts sum to final test rows. Probability values are in `[0,1]`; test IDs and labels join back to actual source rows.
- Validation review capacity is respected under the saved threshold/tie policy; actual test workload is reported without retuning.
- Saved artifact reload succeeds and reproduces predictions. Fresh example outputs contain five rows and no invented observed outcomes.
- Model, source, report, metrics, charts, predictions, logs and notebook are present and registered. Numeric dashboard values agree with the saved metrics/tables.

Document the scope of each check: schema/file checks establish structure, while leakage and metric checks establish specific evaluation properties. None establishes real-world fraud-detection validity.

## Working with a smaller local model

Handle the stages separately: inspect and propose; approve; generate the split/check code; generate training and validation selection; generate frozen test evaluation and artifact reload; generate presentation. Reuse the saved approved contract and bounded execution errors at each step. Ask for one small valid structured response at a time, with dependencies and output paths fixed. Repair only the failed step, rerun its checks and preserve earlier evidence.

Do not promise that one large prompt will make a smaller model equivalent to a stronger coding model. If generation fails, record the failure honestly and use reviewed manual source edits plus a sandbox rerun. The checked-in `scripts/prediction_demo.py --approve-prediction-run` route executes the reviewed Codex-assisted example so the application can still demonstrate a real trained model independently of local generation quality.
