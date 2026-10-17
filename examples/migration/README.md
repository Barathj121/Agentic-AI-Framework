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

`scripts/migration_demo.py --fixture-model --approve-demo` exercises the same
backend and real Docker sandbox with explicitly mocked model responses. That mode
does not demonstrate Gemma inference. Without `--fixture-model`, the script uses
the configured model through Google ADK; `--approve-demo` records demo-only human
approval and converts unresolved ambiguous fields to missing for a partial preview.
