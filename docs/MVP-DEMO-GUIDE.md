# MVP demonstration guide

Demonstrate three workflows inside the same Streamlit application without editing source between demonstrations. Show persisted evidence and distinguish live model reasoning from reviewed fixture input.

## Before the demonstration

On the prepared machine:

```powershell
Set-Location 'C:\Users\bkumarj\Desktop\Agentic-Workbench'
.\scripts\start.ps1 -StartServices
```

Open [http://127.0.0.1:8501](http://127.0.0.1:8501). Allow Rancher Desktop/Docker and Ollama to become ready. This command uses `.venv` when present, otherwise this machine's ignored `local-runtime.json` selects the existing prepared Python environment. On another laptop, complete [setup](PERSONAL-LAPTOP-SETUP.md) first.

In a second PowerShell window, choose the interpreter actually used by the app:

```powershell
Set-Location 'C:\Users\bkumarj\Desktop\Agentic-Workbench'
$workbenchPython = (Get-Content -LiteralPath '.\local-runtime.json' -Raw | ConvertFrom-Json).python_executable
& $workbenchPython scripts\doctor.py
& $workbenchPython scripts\check_model.py --generate --capabilities
```

On a fresh setup use `.\.venv\Scripts\python.exe` instead. If Docker is not on that second terminal's PATH, use its installed executable path or add its directory to that session. The startup script's PATH changes do not change other already open terminals.

Use `.env` with `AGENT_FRAMEWORK=google_adk`, `LLM_PROVIDER=ollama`, and `OLLAMA_MODEL=auto` or the exact installed Gemma tag. `auto` only discovers installed Gemma; it does not download weights. Do not present an unavailable/in-progress model download as a completed capability check. A structured smoke test is not proof of a complete workflow.

Prepare the external image with a ready engine:

```powershell
docker build --pull=false -f examples/external_app/Dockerfile -t workbench-launcher-demo:latest .
```

Its base is the already built `agentic-workbench-sandbox:latest`. The application itself uses only Python's standard library; it is a launcher demonstration, not an implemented customer-analytics product.

## Demo 1 — Migration Studio

### Establish the business question

Use this prompt:

> Assess whether our existing payroll data can satisfy the new payroll application's requirements. Propose mappings, identify gaps, transform the available data, and create target-compatible tables.

Open **Migration Studio** and click **Load synthetic payroll example**. Alternatively upload these files from `examples/migration/`:

| Upload | File |
|---|---|
| Source application data | `legacy_payroll_data.csv` |
| Source metadata, optional | `legacy_payroll_metadata.csv` |
| Target metadata | `target_payroll_metadata.csv` |

The optional metadata is useful for this demo because it records business meaning. Without it, the application can infer types and null observations but does not know salary periods or business equivalence from types alone.

Show the source preview and target requirements. Explain that source data and both metadata sets are persisted in the workspace's DuckDB database and filesystem.

### Generate and review mappings

Click **Generate semantic mappings**. This requests structured mapping proposals from the configured local model through Google ADK. Inspect the actual proposals; do not assume a small model will always choose the right mapping.

The example contains useful points to discuss:

- `worker_code` and `employee_id` describe the same identifier despite different names.
- Source metadata defines `monthly_base_pay`; target metadata requests `annual_base_salary` in the same currency and scope. Multiplying by 12 is supported by those definitions, not just a guessed name match.
- Date and department strings require target type conversion; some synthetic records are deliberately malformed.
- `worker_class` describes engagement category, so it does not establish whether `employment_status` is active or inactive.
- `tax_identifier` is required by the target and absent from the source. It must remain missing until authoritative information is supplied.

Use the editable mapping table. **Source Field(s)** takes a JSON list such as `["monthly_base_pay"]`. Review transformation text and status for every target field. Resolve ambiguous fields using real requirements, or mark them **MISSING**/**REJECTED**. An approved mapping cannot stay ambiguous. The entire plan must have no pending statuses before approval.

Show the fit-gap counts and unresolved required fields. Confidence labels are qualitative model assessments, not probabilities. Check the review confirmation and click **Approve reviewed mapping plan**. Table edits before this action are drafts; this action saves one immutable approval checkpoint.

### Execute and inspect evidence

Click **Generate and execute approved migration**. Straightforward direct mappings can use deterministic SQL without another model call; approved transformation/derived mappings request structured transformation source. Generated code executes through the existing Docker sandbox, not in Streamlit.

Show:

1. Source/target row counts and valid/invalid record counts.
2. Actual rows in **Target data**, including transformed salary values and unresolved nulls.
3. The approved mapping in **Mapping report**.
4. Field checks and record-level conversion/allowed-value/required-value issues in **Validation report**.
5. Inspectable SQL/Python, execution logs and notebook under **Artifacts and export**.
6. Working downloads for CSV, mapping JSON, validation JSON and connector ZIP.

The supplied example is designed to produce a useful **partial migration** because required tax identifiers are unavailable and some values are invalid. Successful Docker execution does not make those records valid. The page should report the actual result; do not describe an incomplete migration as import-ready.

The target table is `results.migrated_data`; record issues persist in `results.validation`. The notebook records saved execution evidence and source references; it is not represented as separately cell-executed unless that occurred.

### Explain the connector

The ZIP is a reusable mapping/transformation source package with metadata, an entrypoint, dependencies and validation requirements. It is not a live endpoint connector, marketplace or vendor-certified import format. Reuse requires compatible source schema, review, and the existing Docker execution context. There is no automatic connector-upload/import feature in this MVP.

To change an already approved plan, create a new migration workspace. This preserves the original approval and execution evidence.

### Optional repeatable integration demonstration

For a predictable engineering check, the CLI can use explicit mocked model responses while running real Docker transformations:

```powershell
& $workbenchPython scripts\migration_demo.py --fixture-model --approve-demo
```

Call this **mocked reasoning with real Docker computation**, not a live Gemma demonstration. It creates a distinctly labelled workspace that can be opened in Migration Studio.

For live model mapping proposals without command-line approval:

```powershell
& $workbenchPython scripts\migration_demo.py
```

Then review the printed workspace in the UI before approval. The script's `--approve-demo` flag explicitly authorizes its synthetic-demo approval behavior; the UI path is preferable when demonstrating human review.

## Demo 2 — Generic Data Workbench

Return Home and click **Open Data Workbench**. Upload `examples/data/customer_sales.csv` or another suitable CSV. Describe a concrete question, for example:

> Analyze sales by region and product category. Show revenue trends, identify which categories changed most, explain data-quality limitations, and save the result tables and charts so I can inspect them.

The wording is an objective, not a claim that every local model will successfully implement it. Use the actual dataset columns and profile when reviewing proposals.

1. Create the workspace and show its profile.
2. Generate proposals and inspect feasibility, assumptions, outputs and validation plans.
3. Approve a suitable proposal through the normal approval control.
4. Open **Execution monitor** to show events, generated code and actual container output.
5. Inspect **Results** and registered tables/charts/notebooks after validation.
6. Reopen it from **Workspace history** to demonstrate persistence.

The solution engine is generic and uses the same workspace, Docker, validation and artifact components as Migration Studio. It is constrained by installed libraries/resources, not a fixed list of solution categories. If generation or execution fails, show that failure and its evidence rather than presenting a saved fixture result as a live success.

Earlier synthetic predictive and transaction-investigation workspaces remain inspectable. They demonstrate reviewed assistant-authored computation and saved-model prediction; their provenance must remain visible when shown.

## Demo 3 — External application hosting

Open **External applications** and expand **Register an external Docker application**.

| Field | Value |
|---|---|
| Application name | `Customer Insights demo` |
| Local Docker image | `workbench-launcher-demo:latest` |
| Internal application port | `8501` |
| Description | `Trusted local launcher demonstration` |

Confirm that you trust the image, then **Register application**. Registration verifies the local image and saves its immutable ID. Click **Launch**, then **Open application**. Show the separate demo page and return to click **Stop**. Refresh status if necessary; the registered record remains available for another launch.

For a teammate's application, substitute its existing trusted image name and actual listening port. No image is pulled or built automatically. The application must bind `0.0.0.0` inside its container. The external URL binds only to this computer's `127.0.0.1` at an automatically assigned host port.

External apps have a read-only filesystem, writable `/tmp`, UID/GID 10001, 2 GB RAM and 2 CPUs, no host mounts or Docker socket, no privileged access and no persistent volumes. Images declaring volumes are unsupported. The default bridge permits outbound requests; the launcher clears inherited Docker proxy variables and passes no host API credentials. Running status means the container process is running, not that a custom readiness endpoint passed.

## End the demo

Stop external applications using their Stop buttons. Cancel active generic runs through the monitor and wait for migration operations to finish. Ctrl+C stops Streamlit; quit Ollama and the selected desktop engine if their memory is no longer needed. Workspaces, model weights and images remain on disk.

Do not claim a complete autonomous live Gemma demo until that exact workflow has actually completed. Keep the configured model ID, approval record, generated files, execution logs and resulting tables as the evidence.
