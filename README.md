# Agentic Data Solution Workbench

**Optional BFSI agent:** [ClariFi financial investigation](docs/CLARIFI.md) adds
insider-trading indicators and shell-company/payment-network analysis through a
registered LangGraph agent, Gemini on Vertex AI and a separate Cloud Run API.
Open **ClariFi Agent** in the sidebar to run both cases with local Gemma
or configured Vertex Gemini. The existing local workflow remains available. Install
`requirements-clarifi.txt` only when using the financial agent; scores are
explainable triage rules and require independent accuracy evaluation.

A local application that turns a CSV and a business problem into proposed computational approaches. You choose and approve a proposal; a configured local model generates code, Docker executes it with resource limits, and the workbench validates and preserves its outputs. Use Ollama by default or Bionic/LM Studio's local OpenAI-compatible model API. Streamlit provides the interface, LangGraph persists the approval workflow, and each workspace has its own DuckDB database.

The workflow is generic: SQL analysis, statistical methods, machine learning and graph analysis share the same builder, sandbox, validation and artifact registry. There are no banking-specific agent branches and no cloud-model fallback. A missing dependency or failed run is reported as a failure, with logs and generated files retained.

## Requirements on Windows

- Windows 11 with virtualization and WSL2 available for Linux containers.
- Python 3.11 recommended; supported host versions are governed by the pinned dependency ranges in `requirements.txt`. The sandbox uses Python 3.11.
- Ollama for Windows and a downloaded local model, or Bionic/LM Studio with its local model server enabled. `llama3.1:8b` is the default Ollama example; another supported local model can be selected in `.env`.
- Docker Desktop using its WSL2 engine and Linux containers, **or** Rancher Desktop using the **Moby/dockerd** container engine. A Docker CLI without a running compatible engine is insufficient.
- A practical starting machine is 32 GB RAM with at least four CPU cores. The default sandbox has an 8 GB memory limit and four CPUs; the local LLM needs memory in addition to the sandbox. CPU-only generation can be slow.
- Internet access is needed for initial Python packages, the Docker image build, and model downloads. Once those dependencies are present, workflow inference and execution are local.

Install Python from [python.org](https://www.python.org/downloads/windows/), Ollama from [ollama.com](https://ollama.com/download/windows), and a container engine from [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) or [Rancher Desktop](https://docs.rancherdesktop.io/getting-started/installation/). Follow [Microsoft's WSL installation guide](https://learn.microsoft.com/en-us/windows/wsl/install) if `wsl --status` does not report a configured WSL2 installation. Installing or enabling virtualization may require administrator access and a restart.

For an existing Bionic/LM Studio installation, use the [model configuration and debugging guide](docs/MODELS-AND-DEBUGGING.md). It explains how to select the exact served Gemma or other model ID, choose Docker Desktop or Rancher, and edit the application from the same checkout. Codex is not required to run or develop this project. A fresh laptop walkthrough is in [PERSONAL-LAPTOP-SETUP.md](docs/PERSONAL-LAPTOP-SETUP.md).

## Setup and startup

Open PowerShell in this repository directory. The setup script creates a repository-local virtual environment, installs the requirements, preserves any existing `.env`, and generates the example CSVs. It builds the sandbox only when explicitly requested.

The following quick start uses the default Ollama provider. If you already serve a model from LM Studio, follow the compatible-provider configuration in the guide above instead of pulling an Ollama model.

```powershell
.\scripts\setup.ps1 -BuildSandbox
ollama pull llama3.1:8b
ollama list
.\.venv\Scripts\python.exe scripts\doctor.py
.\scripts\start.ps1
```

If Python 3.11 is not registered with the Windows `py` launcher, select an installed Python 3.11+ interpreter explicitly:

```powershell
.\scripts\setup.ps1 -PythonExe "C:\path\to\python.exe" -BuildSandbox
```

Development checks on this machine use a Python 3.12 host interpreter; the Docker sandbox remains Python 3.11. `doctor.py` exits with status 1 when the model, container engine or sandbox image is unavailable, and status 0 only when all required services are ready.

To verify actual structured generation through whichever provider you configured, run `.\.venv\Scripts\python.exe scripts\check_model.py --generate`. Without `--generate`, this command checks model availability only. A successful small response is distinct from the complete workflow smoke test below.

Then open [http://localhost:8501](http://localhost:8501). Keep the selected local model server and container engine running. You can install Python dependencies, generate sample data, upload CSVs and inspect profiles while the model downloads. Proposal generation needs the configured model; approved execution also needs Docker and the sandbox image.

On the current prepared machine, run `.\scripts\start.ps1` directly from this repository. It uses the repository's `.venv` when present, otherwise the already prepared `..\..\work\.venv` environment. You can also pass `-PythonExe "C:\path\to\python.exe"` to choose an environment with the requirements installed. For another machine, run setup first.

If PowerShell blocks the local setup script, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that window, or use the manual commands below. Process scope ends when the window closes; it does not change the machine policy.

Manual equivalent:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Only copy when you have not already configured .env:
Copy-Item .env.example .env
docker info
docker build -t agentic-workbench-sandbox:latest -f sandbox/Dockerfile .
.\.venv\Scripts\python.exe examples\generate_data.py
.\.venv\Scripts\python.exe scripts\doctor.py
.\.venv\Scripts\python.exe -m streamlit run app.py
```

## Use the workbench

1. **New workspace:** enter a name, upload a CSV, and describe the desired outcome. Creation saves the original data and creates a profile. Review inferred types, missing values and warnings.
2. **Solution proposals:** request proposals from the local model. Inspect expected outputs, resource estimates, limitations, assumptions and validation strategy. Choose a feasible proposal and explicitly approve it. Rejection is persisted.
3. **Execution monitor:** follow persisted agent events, generated source, execution attempts, stdout/stderr and validation. This screen refreshes every three seconds. The worker is a separate process, so refreshing or closing the browser does not discard the run.
4. **Results:** inspect registered tables, files, code, notebooks, Plotly JSON charts and model metadata. Read-only SQL queries and table previews are bounded. Unknown artifact types remain downloadable. Generated model files are never deserialized in Streamlit.
5. **Workspace history:** reopen any prior profile, proposals, execution evidence or result artifacts after restarting the application.

Approval is required before code generation/execution. Closing Streamlit does not cancel a worker; use **Cancel current run** in the monitor. If a machine restart interrupts a run, reopen the workspace and inspect its status and logs before retrying. Workers use file locks to serialize changes to each workspace.

## Configuration

Copy `.env.example` to `.env` when first setting up and adjust values before starting Streamlit. Existing process environment variables take precedence. Finish or cancel active jobs before changing configuration, then restart Streamlit; already running workers retain their original settings.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama` or `openai_compatible`; explicit selection, no fallback |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama service |
| `OLLAMA_MODEL` | `llama3.1:8b` | Downloaded model tag; change without editing code |
| `LLM_BASE_URL` | `http://localhost:1234/v1` | Compatible local server base URL, including `/v1` |
| `LLM_MODEL` | empty | Exact ID returned by that server's `/v1/models`; required for `openai_compatible` |
| `LLM_API_KEY` | empty | Optional local server authentication token; keep it out of Git |
| `LLM_STRUCTURED_MODE` | `json_schema` | `json_schema` or explicitly selected `json_object`; all responses are validated |
| `LLM_TEMPERATURE` | `0.1` | Generation temperature |
| `LLM_TIMEOUT_SECONDS` | `180` | Inference request timeout |
| `LLM_CONTEXT_TOKENS` | `8192` | Ollama requested context; compatible-provider client budget, with server load context configured separately |
| `LLM_MAX_OUTPUT_TOKENS` | `4096` | Requested output budget |
| `WORKSPACE_ROOT` | `./workspaces` | Persistent workspace location |
| `MAX_UPLOAD_MB` | `100` | Backend CSV upload limit |
| `SANDBOX_IMAGE` | `agentic-workbench-sandbox:latest` | Prebuilt reusable image |
| `SANDBOX_CPUS` | `4` | CPU limit per execution |
| `SANDBOX_MEMORY` | `8g` | Memory limit per execution |
| `SANDBOX_TIMEOUT_SECONDS` | `300` | Execution wall-clock limit |
| `MAX_REPAIR_ATTEMPTS` | `2` | Repairs after the initial attempt, maximum two |

Changing providers or models requires `.env` edits and a restart, not Python source changes or a sandbox image rebuild. For CPU-only inference, `LLM_TIMEOUT_SECONDS=600` is a starting suggestion, not a guarantee; an actual Llama repair request on this CPU exceeded it. When using LM Studio, load the model with at least the configured context budget; a model's advertised 128K capability does not mean the server is currently using 128K. See [configuration examples and the observed CPU limitation](docs/MODELS-AND-DEBUGGING.md#use-a-model-served-by-lm-studio).

Streamlit also has a 100 MB upload limit in `.streamlit/config.toml`; change both limits if intentionally increasing it. UI file downloads are capped at 100 MB, JSON previews at 2 MB, code/text previews at 256 KB, and data previews at 100 rows. These limits protect the host UI from large generated output files. Complete artifacts remain in the workspace.

## Synthetic examples and end-to-end demonstrations

For **actual supervised prediction**, use the [predictive alert-prioritization experiment](examples/prediction/README.md). It trains prevalence, logistic-regression and random-forest candidates on 8,000 synthetic alerts, selects the model using validation average precision, freezes a validation-derived review threshold, and evaluates 1,000 later alerts. Thirty-day gaps ensure earlier outcome labels mature before the next modeling period. It saves a reusable trained model, predictions, comparison metrics, capacity tradeoffs, feature importance, four charts and a model card.

```powershell
.\.venv\Scripts\python.exe scripts\prediction_demo.py --approve-prediction-run
```

Open the resulting **CODEX-ASSISTED PREDICTION** workspace in **Workspace history → Results**. This is reviewed assistant-authored training code executed in Docker; the workbench does not call Codex or a local LLM for this explicit experiment. The outcome is simulated analyst escalation, not proven financial crime. For a reusable staged specification to give a capable coding agent, see the [complete predictive-model master prompt](examples/prediction/MASTER_PROMPT.md); its acceptance criteria describe additional production work as well as this demonstration.

For a ready-to-run visual demonstration, use the [Codex-assisted transaction investigation showcase](examples/showcase/README.md). It analyzes the bundled 10,000 synthetic transactions in Docker and produces a dashboard, customer review queue, transaction evidence, shared-recipient flows, threshold sensitivity and downloadable outputs. The source is explicitly assistant-authored; this demonstration does not call a local or cloud model or establish autonomous model quality.

```powershell
.\.venv\Scripts\python.exe scripts\showcase_demo.py --approve-showcase-run
```

Open the resulting **CODEX-ASSISTED SHOWCASE** in **Workspace history → Results**. The dashboard shows saved computed values only after output validation passes. The same Results interface supports any validated `workbench.dashboard.v1` JSON report with registered charts; there are no banking-specific UI branches.

```powershell
.\.venv\Scripts\python.exe examples\generate_data.py
```

This creates `examples/data/banking_transactions.csv` (10,000 rows), separate `banking_annotations.csv`, `customer_sales.csv` (5,000 rows), and `dataset_info.json`. The generator uses fixed seeds. Banking examples contain unusually large amounts, repeated small transfers, high-frequency activity, a customer behavior shift and a shared destination. Sales examples contain regional category growth and seasonal discounts. Annotations are kept separate from features to avoid accidental label leakage. These patterns are synthetic and do not establish financial crime or real-world predictive quality.

Use the four exact prompts and result checks in [examples/scenarios.md](examples/scenarios.md): SQL-oriented frequent transfers, customer anomaly scoring, account relationship analysis, and non-banking sales analysis. The same workflow handles every scenario.

The optional live-model smoke script uses six synthetic rows with a known total of 135. The first command creates and profiles a workspace, invokes the configured real model, and prints proposals. Review them before running the explicit approval command printed by the script:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_test.py --real-model
# Replace these values with the workspace and proposal IDs printed above:
.\.venv\Scripts\python.exe scripts\smoke_test.py --real-model --workspace "uc_..." --approve "proposal_..."
```

The second command generates and executes code in Docker, validates actual outputs and preserves the workspace. It does not return success unless the workspace completes. This is an opt-in smoke test; live model behavior is never used as a deterministic unit-test dependency.

## Tests and verification status

```powershell
.\.venv\Scripts\python.exe -m pytest tests -v
# Opt in to Docker integration checks after building the image:
$env:RUN_DOCKER_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest tests -v -m docker
Remove-Item Env:RUN_DOCKER_TESTS -ErrorAction SilentlyContinue
```

The standard suite uses deterministic test adapters for model responses. Docker integration tests are separately marked and require the opt-in environment variable. Authored-response adapters are confined to tests, the explicitly labelled fixture demo, and the separately labelled Codex-assisted showcase and predictive experiment; production never fabricates proposal or execution results when a dependency is missing. These explicit demonstrations supply reviewed source and contracts and record that no local model was called. UI tests use Streamlit AppTest without requiring live services.

For an explicitly labelled fixture demonstration across all four computational approaches, run:

```powershell
.\.venv\Scripts\python.exe scripts\fixture_demo.py --scenario all --approve-fixture-run
```

This uses reviewed fixed model-response fixtures for SQL, sklearn anomaly scoring, NetworkX graph analysis and non-banking sales analysis. All computation, model loading and output inspection still run in real Docker containers through the same generic workflow. It does not call a real LLM or demonstrate local-model reasoning. Each workspace is marked `FIXTURE DEMO` and saves `metadata/demo_fixture.json`. Without `--approve-fixture-run`, it stops at the normal approval checkpoint. `tests/test_demo_e2e.py` validates these same four cases with real Docker when opted in.

**Delivery verification — 15 October 2026:** 170 tests passed across the recorded batches: the current 155-test non-Docker suite and 15 actual Docker integration tests from the prior run on the unchanged sandbox implementation. The Docker tests were not rerun for the dashboard/showcase additions. These cover provider configuration, structured responses, workflow approval and repair, output validation, UI behavior, independent smoke-result checks and manual edited-code execution. The four fixture demonstrations execute SQL, sklearn anomaly scoring, NetworkX graph analysis and sales/Plotly in restricted Docker containers; fixture responses do not establish live-model reasoning quality.

The host was tested with Python 3.12.14; the image uses Python 3.11.17. Exact dependency inventories are in `requirements-tested-windows-py312.txt` and `sandbox/requirements-tested-linux-py311.txt`. `verification.json` records the delivery evidence. JUnit reports are kept locally and excluded from Git and the source ZIP. Streamlit AppTest is included in the test suite; HTTP health returned 200. The running showcase dashboard was also visually inspected in a browser, including its four metrics, findings, four Plotly charts and customer review table.

**Codex-assisted showcase verification:** The reviewed synthetic analysis completed in real Docker in 4.516 seconds, with 42 validation checks and four additional independent SQL checks passing; 24 artifacts were registered. It analyzed 10,000 transactions for 532 customers, retained 483 evidence transactions and identified 106 customers meeting at least one descriptive review rule. Independently checked findings include one customer's 100 transfers in a clock hour, a recipient with 25 source accounts and 100 transfers, and five customers making 20 similar modest transfers each. These are synthetic descriptive patterns, not fraud labels or a demonstration of autonomous local-model reasoning. [showcase-verification.json](showcase-verification.json) records the actual provenance, computation, validation and limitations.

**Predictive-model verification:** The supervised experiment completed in real Docker in 7.109 seconds. Logistic regression won on validation average precision. On the 1,000 untouched later alerts, average precision was 0.444 against prevalence 0.170, and ROC-AUC was 0.727. The frozen threshold prioritized 118 alerts: 64 simulated escalations and 54 false positives, giving 54.2% precision and 37.6% recall; 106 simulated escalations remained outside that queue. At the separate fixed capacity of exactly 100 reviewed alerts, the learned model captured 57 simulated escalations versus 45 for the fixed amount/activity heuristic. Reloading the saved pipeline reproduced all 1,000 test scores exactly, and five new cases with unknown outcomes were scored. Thirteen independent SQL checks reconciled the saved predictions, labels, confusion counts, average precision and Brier score. [prediction-verification.json](prediction-verification.json) preserves the computation and provenance. These results describe the synthetic label generator; they do not establish real-world FCCM effectiveness or calibrated crime probabilities.

**Live model verification:** The installed Llama3.1:8b passed text generation and schema-validated structured responses. The new compatible-API adapter also passed a real structured request against Ollama's `/v1` endpoint. Bionic/Gemma itself was not tested on this host. The attempted complete live-model workflows did not pass; see `live-workflow-verification.json` for the actual failures. The first CPU run failed with generated SQL quoting syntax and then a 600-second repair timeout. Its original failure evidence remains intact. A separate manually corrected recovery is recorded explicitly as manual work, not autonomous model success. Model weights, environments and runtime workspaces are excluded from the source archive. Use `scripts/check_model.py --generate` and the optional live smoke test to check your selected local model.

## Repository layout

```text
app.py                       Streamlit workflow and artifact viewer
.env.example                 Local configuration template
.streamlit/config.toml       Local binding, upload limit and theme
requirements.txt             Host runtime and test dependencies
workbench/
  settings.py                Environment configuration
  schemas.py                 Strict structured contracts
  workspace.py               Workspace paths, metadata and event persistence
  data.py                    CSV ingestion and dataset profiling
  query.py                   Bounded read-only DuckDB queries
  llm.py                     Structured local model adapters
  prompts.py                 Generic agent instructions
  workflow.py                Checkpointed LangGraph workflow and approval gate
  service.py                 UI-facing API and durable worker launch
  worker.py                  Background workflow process entry point
  sandbox.py                 Container lifecycle and execution isolation
  artifacts.py               Evidence validation and artifact registration
  debug.py                   Review and sandbox rerun in a fresh debug workspace
ui/helpers.py                Safe bounded artifact previews
sandbox/                     Dockerfile, preinstalled libraries, trusted runner
examples/                    Seeded generators and demonstration prompts
scripts/                     Setup, startup, model checks, smoke test and debug CLI
docs/                        Laptop setup, model selection and debugging
tests/                       Foundation, contracts, UI and Docker checks
workspaces/                  Runtime data, created on first use
```

Each workspace keeps the original CSV in `input/source.csv`; raw, staging, feature, result and audit schemas in `database/workspace.duckdb`; generated modules under `generated/`; reusable models under `models/`; notebooks under `notebooks/`; and other files under `results/`. `metadata/` holds the dataset profile, proposals, selected plan, execution and validation evidence, artifact manifest and SQLite LangGraph checkpoints. A private pristine database snapshot ensures every attempt starts from the same input. Earlier derived databases and output files are archived in `metadata/prior_outputs/`, preventing stale results from satisfying a retry. `logs/agent.jsonl` is the durable activity stream. Never edit a workspace database while its sandbox execution is active.

## Execution and trust boundaries

Generated programs run as an unprivileged user in a Linux container with a read-only root, no network, dropped capabilities, a process limit, resource limits, a timeout and temporary storage. Input and generated source are read-only mounts. Only the workspace's database, results, models and notebooks are writable. Metadata, logs, host environment variables and the Docker socket are not mounted. The host captures logs and controls container cleanup. If cleanup cannot be confirmed, the container identifier is retained and subsequent execution/database access fails closed until removal is confirmed. Serialized model loading runs in a separate child interpreter inside the inspection container so it cannot replace the trusted aggregate validation report.

The image is built ahead of time with a fixed set of analysis libraries. Agents cannot install arbitrary dependencies at runtime; add a reviewed dependency to `sandbox/requirements.txt` and rebuild if a future use case requires it. Proposal feasibility should account for available libraries.

The Docker image contains Python, its approved libraries and the trusted runner. The application source stays in this checkout, and generated solution files stay in each workspace. Editing `app.py` or `workbench/` generally requires restarting the app, not rebuilding Docker. Changes to `sandbox/Dockerfile`, `sandbox/requirements.txt` or `sandbox/runner.py` require an image rebuild. The [debugging guide](docs/MODELS-AND-DEBUGGING.md#edit-and-debug-the-code) maps source files and actual workspace logs.

Success requires both successful execution and checks against real outputs. The builder may repair a failed implementation at most twice. All failed attempts remain inspectable. ML models are loaded only inside the sandbox. The Streamlit UI reads metadata and text, uses bounded read-only queries, and does not execute generated modules, notebook cells or serialized models. Registered Plotly JSON is rendered only when it contains no external resource URLs.

Notebooks are inspectable, reproducible artifacts that include approved plans, implementation references and actual saved execution/validation evidence. A generated notebook is not represented as cell-executed unless it actually was executed. The authoritative implementation remains the generated Python/SQL files.

This MVP is a single-user local tool. Bind Streamlit to loopback as configured; it has no authentication or multi-user authorization. Container isolation is a practical boundary, not a guarantee against every container/runtime vulnerability. Maintain a current container runtime and keep sensitive unrelated data outside workspace mounts.

## Troubleshooting

| Symptom | What to check or change |
|---|---|
| `docker` exists but daemon is unavailable | Start the engine. Run `docker context ls` and `docker info`. For Rancher, select Moby/dockerd; containerd alone does not provide the required Docker API. Ensure the selected context points to the intended running engine. |
| WSL2 is unavailable | Run `wsl --status`; follow Microsoft's WSL2 setup guide, enable required virtualization features, then restart if requested. |
| Sandbox image is missing | Run `docker build -t agentic-workbench-sandbox:latest -f sandbox/Dockerfile .` with a running engine. Match the image tag to `SANDBOX_IMAGE`. |
| Ollama is unavailable | Open Ollama or run `ollama serve`. Check the configured base URL. A port-in-use error usually means a server is already running. |
| Model is missing | Run `ollama list` and then `ollama pull` with the exact configured tag. The application does not silently use a different model. |
| LM Studio model is missing or mismatched | Start its local server, query `/v1/models` and copy the exact returned ID into `LLM_MODEL`; use `LLM_PROVIDER=openai_compatible`. |
| Compatible server rejects structured output | Prefer `LLM_STRUCTURED_MODE=json_schema`. Use `json_object` only when deliberately selecting a server-supported JSON mode; the workbench still validates the full response and does not silently switch modes. |
| Inference times out | CPU inference can exceed even 600 seconds, as observed in a Llama repair request. Increasing `LLM_TIMEOUT_SECONDS` permits a longer wait but does not guarantee completion. Reduce input complexity or use a capable model suited to your hardware. A larger context/model also needs more RAM. |
| Structured output fails repeatedly | Review the local model error and logs. Simplify the problem or use a stronger local model. The application must report this limitation rather than fabricate structured results. |
| Memory exhaustion / container OOM | Inspect the execution metadata. Reduce dataset/model workload or intentionally change `SANDBOX_MEMORY`; keep enough RAM for Windows, the model and Docker together. |
| Python package conflicts | Use the repository-local `.venv` and its explicit Python path. Avoid installing these requirements into a shared system interpreter. |
| Read-only query rejected | Use a single SELECT over existing workspace tables. Filesystem functions, extensions, writes and external database access are deliberately unsupported. |
| Workspace appears busy | An independent worker may still be running. Inspect the monitor, wait, or cancel it. Do not remove lock files to force concurrent writes. |
| Browser preview is too large | Download the artifact within the UI limit or inspect the saved local file; previews are intentionally bounded. |

### Ollama download and a stale proxy

PowerShell environment variables use `$env:HTTPS_PROXY`, not `$https_proxy`. Ollama's running server retains the environment it was started with, so clearing variables in a new client shell alone may not change its downloads. To remove proxy settings for temporary sessions only:

1. Quit Ollama from the Windows system tray.
2. In a PowerShell window, run:

   ```powershell
   Remove-Item Env:HTTP_PROXY, Env:HTTPS_PROXY, Env:ALL_PROXY -ErrorAction SilentlyContinue
   ollama serve
   ```

3. Leave it running. In a second PowerShell window, run:

   ```powershell
   Remove-Item Env:HTTP_PROXY, Env:HTTPS_PROXY, Env:ALL_PROXY -ErrorAction SilentlyContinue
   ollama pull llama3.1:8b
   ollama list
   ```

4. After downloading, stop the temporary server with Ctrl+C if desired. The saved model remains on disk and Windows' persistent environment settings are unchanged.

If your network requires a corporate VPN/proxy, connect to it or use a proxy hostname that resolves on that network. Clearing variables cannot create direct internet access where it is unavailable. An alternative GGUF model downloaded from a trusted model repository can be imported into Ollama with a reviewed Modelfile. When configured for Ollama, the workbench calls that local API; it does not load arbitrary Hugging Face model formats in its host process. Bionic/LM Studio users can instead use their existing local API as documented above. See [Ollama import documentation](https://docs.ollama.com/import) and [Windows configuration](https://docs.ollama.com/windows).

## Known limitations and next improvements

- Small local models can produce weak plans or invalid code; a bounded repair loop cannot guarantee all natural-language problems will be solvable.
- The MVP accepts one CSV per workspace and uses one local process per requested workflow operation. It has no distributed scheduling, authentication or collaboration.
- Data profiling, previews, LLM context and SQL results are bounded. Large-data throughput is not the target of this MVP.
- CSVs are limited to 200 columns. Additional approval instructions can refine implementation within the selected output contract; changes to objectives, required outputs or schemas need new proposals and approval.
- This machine's WSL kernel lacks aggregate cgroup swap accounting. RAM limits, zero swappiness and inherited per-process address-space limits are applied and memory exhaustion was tested; an aggregate swap cap is not claimed. Workspace output directories do not have a global disk quota, although per-file size, process count, execution time and inventory are bounded.
- The default sandbox includes the listed CPU analysis libraries but not PyTorch. A GPU or deep-learning runtime is not required for the MVP; unsupported dependencies are reported before execution.
- High-quality analytical correctness still depends on the approved plan, generated checks and human review. Structural checks and computed metrics cannot prove that an approach answers every business question correctly.
- A successful synthetic demonstration does not validate a method for real financial, clinical or operational decisions. Domain review and appropriate external evaluation are separate work.
- Notebook generation records real evidence, but automatic execution of every generated notebook cell is not a prerequisite for completion. Use the saved code and sandbox to reproduce results.
- Future work could add dataset joins, richer feasibility estimation, more artifact previews, stronger semantic correctness checks, migration/versioning of workspace contracts, and a configurable reviewed dependency catalog.
