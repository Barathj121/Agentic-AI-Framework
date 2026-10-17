# Supervised alert prediction demonstration

This example trains and evaluates an actual prediction model on synthetic labeled data. Its target is a simulated analyst escalation outcome. It is an educational example of supervised learning and reproducible evaluation; it does not establish that a model detects real fraud.

The dataset, training program and demonstration were authored with Codex assistance. The local Ollama model did not generate this implementation. Training, evaluation, model loading and prediction run inside the Docker sandbox.

## Run it

From the repository root, with the Python environment and sandbox image ready:

```powershell
.\.venv\Scripts\python.exe scripts\prediction_demo.py --approve-prediction-run
```

The explicit flag approves the reviewed example program for sandbox execution. The command reports the new workspace, execution evidence and registered outputs. Open that workspace in the application's Workspace history, then Results. Inspect the saved program, notebook, model, metrics and predictions alongside the dashboard.

## Predict a new alert

In the completed prediction workspace, open **Results → Predict a new alert**. Enter the ten feature values (or start with a preset), then click **Predict this alert**. Each submission loads the saved trained model in a fresh Docker sandbox and returns a new score, the saved threshold and a review decision. It does not retrain the model or call Ollama. The last scored result identifies its submitted inputs and scoring time; editing the form requires another click to get a new prediction. This is on-demand inference, not a connection to a live transaction feed.

The earlier **Example Predictions** table contains five saved inference examples. `escalation_score` and `review_recommended` are their predictions. `outcome = UNKNOWN` means those examples have no observed investigation outcome to compare against. It does not mean the model failed to predict. Scores concern the synthetic escalation target and are not calibrated probabilities of crime.

To rebuild only the synthetic input dataset, without training a model:

```powershell
.\.venv\Scripts\python.exe examples\prediction\generate_data.py
```

The generator writes `synthetic_alerts.csv` and `dataset_metadata.json` beside itself. It uses NumPy, fixed independent random streams and no network. No real customer information is included. Model training belongs inside the sandbox, not in this generator.

## Data and target

There are 8,000 rows: 20 independent customer alerts on each of 400 days, from January 1, 2025 through February 4, 2026, in UTC. Every alert ID and customer ID is unique. The fixed generated file has 1,384 positive outcomes, a 17.3% positive rate.

`confirmed_escalation` is a binary, stochastically simulated analyst outcome. `label_available_at` is exactly 30 days after `alert_time`. Labels are only considered observable from that time; the complete demonstration dataset is observed through March 6, 2026. Neither the target nor its availability timestamp is an input to the fitted model.

The ten predictor fields are stipulated snapshots known at alert time:

| Predictor | Meaning |
|---|---|
| `amount_log` | Natural logarithm of one plus alert amount, in synthetic currency units. |
| `amount_to_median` | Alert amount divided by the customer's historical median amount. |
| `transfers_24h` | Prior transfer count during 24 hours. |
| `unique_recipients_7d` | Unique prior recipients during 7 days. |
| `cross_border_ratio_30d` | Cross-border share of prior transfers during 30 days. |
| `new_recipient_ratio_7d` | Share of prior transfers to newly observed recipients during 7 days. |
| `night_transfer_ratio_7d` | Night-time share of prior transfers during 7 days. |
| `account_age_days` | Account age at alert time. |
| `prior_alerts_180d` | Earlier alerts known during 180 days, excluding the current alert's outcome. |
| `inflow_outflow_ratio_7d` | Prior incoming amount divided by prior outgoing amount during 7 days. |

`alert_id`, `customer_id`, `alert_time`, `label_available_at`, `day_offset` and `confirmed_escalation` are explicitly excluded from the feature matrix. IDs are retained only for evidence joins and deterministic ranking tie order.

## Chronological evaluation

| Partition | Day offsets | Alert dates, inclusive | Rows |
|---|---|---|---:|
| Training | `0 <= d < 230` | Jan 1–Aug 18, 2025 | 4,600 |
| Excluded maturity gap | `230 <= d < 260` | Aug 19–Sep 17, 2025 | 600 |
| Validation | `260 <= d < 320` | Sep 18–Nov 16, 2025 | 1,200 |
| Excluded maturity gap | `320 <= d < 350` | Nov 17–Dec 16, 2025 | 600 |
| Final test | `350 <= d < 400` | Dec 17, 2025–Feb 4, 2026 | 1,000 |

Each earlier partition's labels have matured before the next modeling decision period begins. The two gaps, totaling 1,200 rows, are excluded from fitting, model selection, threshold selection and final test evaluation. Training fits preprocessing and candidate models. Validation chooses the candidate and freezes a review threshold under a 10% validation review-capacity constraint. The final test is used once for honest evaluation after those choices are fixed.

A frozen threshold may select a different fraction of the test period: that observed workload must be reported, not silently forced back to 10%. A separate top-10% ranking metric can describe fixed-capacity prioritization, provided it is clearly distinguished from predictions made by the frozen threshold.

## How labels are simulated

Independent normal latent activity and latent risk produce correlated features with additional random variation. Outcome generation uses a nonlinear logistic formula involving observable features, an interaction between unusually large relative amounts and new-recipient share, and unobserved Gaussian noise. A separate Bernoulli draw produces the binary outcome. Thus similar feature snapshots can receive different outcomes.

The full formula, seeds, bounds, feature definitions and split dates are recorded in `dataset_metadata.json` and implemented in `generate_data.py`. The generator does not export latent variables, true probabilities or outcome noise into the CSV. Its positive class is not a thresholded copy of one input column. No identifier or date enters the outcome formula.

## What the evidence should demonstrate

The saved training program fits candidate estimators on training rows, compares them on validation rows and persists the selected fitted estimator. Reports include baselines, probability and ranking metrics, the validation-selected threshold, final test confusion counts and the observed review workload. The saved artifact is reloaded and used to reproduce predictions and score fresh unlabeled feature snapshots.

The reviewed implementation compares a train-prevalence dummy, logistic regression and a random forest. Logistic regression uses median imputation, training-only standardization, `C=1` and at most 1,200 iterations. The forest uses 200 trees, maximum depth 9, minimum leaf size 12 and one worker. Parameters are fixed before evaluation; maximum validation average precision chooses the fitted candidate. A fixed amount/activity heuristic is also included as a separate ranking comparator. The model is not refitted on validation or test rows.

The generated workspace contains these outputs:

| Output | What to inspect |
|---|---|
| `models/alert_prioritizer.joblib` | Fitted pipeline, feature order, frozen threshold and selection metadata. Load only inside the sandbox. |
| `results/test_predictions.csv` and `results.test_predictions` | All 1,000 held-out predictions, source IDs, observed simulated labels and review decisions. |
| `results/model_comparison.csv` and `results.model_comparison` | Candidate/baseline validation and held-out comparisons. |
| `results/threshold_tradeoff.csv` and `results.threshold_tradeoff` | Descriptive review-capacity tradeoffs, with validation-derived thresholds identified. |
| `results/feature_importance.csv` and `results.feature_importance` | Post-hoc permutation importance on the frozen model; not used to tune it. |
| `results/example_predictions.csv` and `results.example_predictions` | Five fresh constructed cases scored from the reloaded artifact; true outcomes are unknown. |
| `results/split_audit.csv` and `results.split_audit` | Partition dates, row counts and label-maturity evidence. |
| `results/metrics.json` and `results/model_card.md` | Definitions, full computed evaluation, selection policy and limitations. |
| `results/dashboard.json` | Validated overview with links to four registered Plotly charts. |
| `results/precision_recall_chart.json` | Held-out precision-recall comparisons. |
| `results/capacity_lift_chart.json` | Same-capacity ranking comparison against the fixed heuristic. |
| `results/feature_importance_chart.json` | Permutation importance, including uncertainty across repeated shuffles. |
| `results/score_distribution_chart.json` | Held-out score overlap and the frozen review threshold. |

The model card and saved source are authoritative for the executed run. The longer prompt below is an acceptance contract for rebuilding or extending this workflow; it is not a substitute for inspecting recorded checks.

Read [MASTER_PROMPT.md](MASTER_PROMPT.md) for the complete staged contract you can give another coding agent or a compatible local model. A small model should handle one stage at a time, with schema checks and execution feedback between stages. A detailed prompt cannot guarantee that a smaller model will generate a correct complete implementation in one request.

## Limitations

- This is entirely synthetic. A strong score demonstrates learning this simulator's signal, not readiness for a real institution.
- The feature lookback meanings are assumed. They are not independently reconstructed from a raw transaction ledger.
- The days are stationary draws, so time splitting demonstrates evaluation discipline but not robustness to operational drift.
- All customers are unique. Repeated-customer dependence, feedback loops and real analyst selection bias are absent.
- Outcome timing is a fixed assumption. Production label delays, missing outcomes and censoring need separate treatment.
- Feature importance describes predictive behavior of the fitted model; it does not establish causality.
- Predictions estimate simulated escalation likelihood. They must not be described as verified fraud probabilities.

