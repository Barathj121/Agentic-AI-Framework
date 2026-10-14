# Codex-assisted transaction investigation showcase

This is an explicitly authored demonstration of the workbench's execution and evidence pipeline. Codex prepared the analysis source in this repository; the application does **not** call Codex, a cloud model, or a local model for this showcase. The normal application continues to use the configured local model.

The bundled 10,000-row synthetic transaction dataset is analyzed inside the existing Docker sandbox. Results include an explainable customer review queue, exact supporting transaction IDs, a shared-recipient flow diagram, activity timeline, four interactive charts, seven queryable tables and CSV exports, threshold sensitivity, a short investigation brief, and a notebook containing execution evidence.

Run from the repository root with the sandbox image already built and Docker running:

```powershell
# Prepare the plan for review, without executing analysis:
.\.venv\Scripts\python.exe scripts\showcase_demo.py

# Create and execute an explicitly approved showcase in a new workspace:
.\.venv\Scripts\python.exe scripts\showcase_demo.py --approve-showcase-run
```

Open the resulting **CODEX-ASSISTED SHOWCASE** workspace in the application. Use **Results** for the dashboard and artifacts. Source is saved as `generated/solution.py` in that workspace. The original reviewed source is [solution.py](solution.py). The adapter is [showcase_demo.py](../../scripts/showcase_demo.py).

The same investigation request can be tried with a capable local model through the normal upload-and-propose flow:

> Build an explainable transaction investigation workbench from this synthetic dataset. Find concentrated transfer bursts, repeated modest payments, unusually large amounts relative to each customer's own baseline, changes in spending, and accounts connected through common recipients. Rank customers for human review; retain exact supporting transaction IDs. Show a connected-money-flow diagram, activity timeline, explainable ranking and signal coverage. Save queryable tables, CSVs, a concise investigation brief, threshold sensitivity and a reproducible notebook. This is descriptive exploration of synthetic data, not a fraud prediction or accusation.

Use `examples/data/banking_transactions.csv`. The separate annotation file is never loaded by this analysis. Local-model output quality and speed are separate from the reproducible authored showcase.

Review points equal 20 times the number of selected descriptive signals. They are not probabilities or financial-crime labels. Thresholds and sensitivity are persisted in `results/showcase_summary.json`. Sensitivity changes the burst, large-amount and recipient thresholds by ±20%; repeated-payment and spending-change rules remain fixed. Amounts are comparable synthetic units; no currency conversion is available. The notebook records module execution but its cells are not themselves executed.
