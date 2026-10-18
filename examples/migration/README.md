# Migration Studio example

All eight employee/payroll records are synthetic. Email addresses use the reserved
`.invalid` domain. No employee data or verified vendor import format is included.

In the application, choose **Migration Studio → Load synthetic payroll example**.
Generate semantic mappings using the configured local Gemma model, review the
table, and explicitly approve the mapping plan. Resolve employment-status
ambiguity by marking that field missing unless additional business data exists.
The tax identifier is absent and must remain missing.

Demo prompt:

> Assess whether our existing payroll data can satisfy the new payroll application's requirements. Propose mappings, identify gaps, transform the available data, and create target-compatible tables.

The source metadata supports monthly-to-annual base salary conversion. It does
not support deriving tax identifiers or active employment status. The data also
contains one invalid date, one non-numeric department code, one unsupported country
code and one missing optional email. A correctly executed demonstration therefore
produces a **partial/incomplete migration**, not a fabricated fully ready result.
Record-level validation issues are materialized in `results.validation`.

Download the actual target CSV, approved mapping, validation report, transformation
source, notebook, and reusable connector ZIP from the result page.

Use **Transformation / review instructions** to describe any conversion, such as
trimming and uppercasing a code or converting monthly pay to annual pay. Editing
those instructions on an approved DIRECT suggestion promotes it to TRANSFORMATION;
unchanged direct-copy descriptions keep their original meaning. Instructions are
saved with the approval and passed to the builder. MISSING decisions take priority:
the framework creates those target columns as typed NULLs without model inference.
Targets omitted by the model are also shown as MISSING rows for review.

For an input date such as `14-08-2024`, choose **TRANSFORMATION** and describe
the input format in **Transformation**, for example: "Parse DD-MM-YYYY input as
a date; flag invalid values." The builder receives this approved correction even
when a metadata hint says ISO. DuckDB uses `%d-%m-%Y` for this input format;
common `DD-MM-YYYY` masks in generated SQL are translated without changing the
day/month order. Invalid conversions are reported against individual records.

Choose **MISSING** for unavailable fields; old source selections are cleared on
approval. Missing required fields make the output incomplete but do not block
the partial CSV export. Results and export tabs remain available after approval;
failed executions explain the next step and keep technical details available.
Use **Retry approved migration** when generation or the runtime needs another
attempt. To correct a decision, choose **Edit approved mappings**, change the
existing review table, and approve it again before executing. The uploaded data
stays in the same workspace, and the previous approval is retained for reference.
Earlier results are no longer presented as validated for the revised mapping.

Use **Migration history** to open the saved migrations list. Each entry shows
its status and available record counts; **Open workspace** restores its review,
results, and exports. Failed and incomplete migrations remain in that list.

`scripts/migration_demo.py --fixture-model --approve-demo` exercises the same
backend and real Docker sandbox with explicitly mocked model responses. That mode
does not demonstrate Gemma inference. Without `--fixture-model`, the script uses
the configured model through Google ADK; `--approve-demo` records demo-only human
approval and converts unresolved ambiguous fields to missing for a partial preview.
