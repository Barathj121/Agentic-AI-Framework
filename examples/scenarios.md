# Reproducible demonstration scenarios

Generate the CSVs with `python examples/generate_data.py`. All records are synthetic, with seed 42 by default. Run every scenario through the same upload → proposals → approval → sandbox → validation workflow. Approach selection is performed by the local model; these examples add no scenario-specific code to the agents.

## A. Frequent transactions and queryable outputs

Upload `examples/data/banking_transactions.csv` and use:

> Identify customers making unusually frequent transfers and create a queryable result. Prefer a transparent SQL approach if suitable. Compare customer activity by day and one-hour window, document chosen thresholds, and retain transaction identifiers that support each finding. Save a ranked result table and an inspectable notebook. Explain missing data and limits; this synthetic dataset cannot establish financial crime.

Review whether a proposal uses the available timestamps and identifiers. Approve a feasible one. After completion, open the registered tables, inspect the actual validation checks, preview generated SQL/Python, and download the notebook.

## B. Unusual customer behavior

Upload the same transaction CSV and use:

> Identify unusual customer behavior using a data-driven approach appropriate to this dataset. Aggregate meaningful customer features, avoid treating identifiers as numeric measurements, and consider an unsupervised method if there are no labels. Save customer-level anomaly scores, fitted preprocessing and model artifacts where applicable, computed diagnostics, and a notebook. Use a reproducible seed, explain feature scaling, and report limitations. Do not invent ground-truth accuracy.

Do not upload `banking_annotations.csv` as training features. The separate annotations identify injected synthetic patterns and may support a separate evaluation. They are not real fraud labels. Check that the proposal's outputs include both scores and evidence about how they were produced. A small local model may need a simpler prompt or a more capable replacement.

## C. Transaction relationships

Upload the same transaction CSV and use:

> Analyze transfers between account_id and destination_account to discover highly connected accounts and unusual transaction relationships. Treat transactions as directed edges and choose suitable graph statistics. Produce queryable node and edge results, include actual computed connectivity measures, and describe the role of repeated transfers. Save a notebook explaining the analysis and its limitations. Do not infer criminal intent from connectivity.

The seeded hub is account `A0999`; inspect whether the computed output recognizes its incoming connections. This is a verification hint, not a required agent output or an implementation branch.

## D. Non-banking sales analysis

Upload `examples/data/customer_sales.csv` and use:

> Analyze monthly revenue and sales quantity by region and product category. Identify the largest changes and compare Q4 discounts with earlier quarters. Create queryable monthly summaries, a customer summary, and a Plotly JSON chart specification. Validate revenue against quantity × unit_price × (1 − discount_rate) allowing for rounding. Save reusable code and a notebook with computed findings and limitations.

The generator increases South-region electronics quantities from July and uses higher Q4 discounts. These are synthetic patterns for checking whether an analysis finds the intended signal.

## Checking a completed demonstration

1. Confirm the workspace status is `COMPLETED`, not merely that a process exited successfully.
2. Open **Results → Validation evidence** and inspect the executed checks.
3. Open a registered result table and compare a few rows with the source data.
4. Inspect generated code and the notebook. Notebook cells without execution counts are explicitly unexecuted; saved execution evidence is distinct from notebook-cell execution.
5. Reload the browser or restart Streamlit, then reopen the workspace from history. Outputs must persist.

Model-generated proposals are hypotheses. Real computed outputs and validation evidence determine whether a run succeeded. Missing Docker or an unavailable local model should produce a clear error, never substituted mock results.
