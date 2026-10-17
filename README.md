# Agentic Data & Application Workbench

A local Python application for building, executing and inspecting data-driven solutions. Its home page brings together **Migration Studio**, the existing **Data Workbench**, and a launcher for **trusted local Docker applications**.

Google ADK runs agent execution and structured reasoning. Gemma is served locally through Ollama; Streamlit provides the UI, DuckDB stores workspace data, and Docker executes generated code. No Gemini API key, paid inference service, cloud deployment or separate backend is required. Results and exports come from executed code, not conversational answers.

## Start the Desktop checkout

Open PowerShell:

```powershell
Set-Location 'C:\Users\bkumarj\Desktop\Agentic-Workbench'
.\scripts\setup.ps1 -BuildSandbox  # First run, or after removing the old Python environment
.\scripts\start.ps1 -StartServices
```

Open [http://127.0.0.1:8501](http://127.0.0.1:8501). Keep the terminal open; Ctrl+C stops Streamlit. The switch starts installed Ollama and a Docker desktop engine if needed, but does not wait for readiness. Allow the engine to finish starting. This machine uses Rancher Desktop with Moby/dockerd.

If both Docker Desktop and Rancher Desktop are installed, start your preferred engine first; automatic startup otherwise checks Rancher first. Use the Docker context belonging to that engine.

The project lives on the Desktop. Setup creates a repository-local `.venv`. The temporary Codex runtime is not part of the source handoff; run setup after cleanup. An explicit interpreter is also supported:

```powershell
.\scripts\start.ps1 -PythonExe 'C:\path\to\prepared\python.exe' -StartServices
```

For checks using the repository's environment:

```powershell
$workbenchPython = '.\.venv\Scripts\python.exe'
& $workbenchPython scripts\doctor.py
& $workbenchPython scripts\check_model.py --generate --capabilities
```

After setup, normal restarts need no dependency reinstall, model download or image rebuild. Start Docker Desktop/Rancher and Ollama, then run `scripts\start.ps1`.

## Set up another Windows computer

Use the current source checkout, Python 3.11 or newer, Ollama, and one Linux container engine: Docker Desktop with WSL2 or Rancher Desktop with Moby/dockerd. The intended machine has 32 GB RAM and a CPU; a GPU is not required. Initial packages, image construction and model downloads need network access.

Clone the repository (or run `git pull` in an existing checkout), then set it up with the container engine ready:

```powershell
git clone https://github.com/Barathj121/Agentic-AI-Framework.git
Set-Location Agentic-AI-Framework
.\scripts\setup.ps1 -BuildSandbox
ollama pull gemma3:4b
ollama list
```

Setup creates `.venv`, installs requirements, preserves an existing `.env`, generates the existing data examples, and builds the sandbox when requested. Pass `-PythonExe 'C:\path\to\python.exe'` if the default `py -3.11` interpreter is unavailable.

Choose an installed Gemma tag in `.env`, or leave `OLLAMA_MODEL=auto` to discover one. **auto is an application setting, not an Ollama model to download.** It does not silently select a non-Gemma model or a cloud model. If no Gemma is installed, select and download a suitable variant first; the workbench never downloads models automatically.

The download command above is an example using the verified `gemma3:4b`; skip it if your chosen Gemma is already installed. For the CPU demonstration, set these entries in `.env`:

```dotenv
AGENT_FRAMEWORK=google_adk
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=gemma3:4b
LLM_TEMPERATURE=0.1
LLM_TIMEOUT_SECONDS=600
LLM_CONTEXT_TOKENS=8192
LLM_MAX_OUTPUT_TOKENS=2048
```

Use the exact tag shown by your own `ollama list`. No source-code edit or Gemini key is needed to change the local model.

```powershell
.\.venv\Scripts\python.exe scripts\doctor.py
.\.venv\Scripts\python.exe scripts\check_model.py --generate --capabilities
.\scripts\start.ps1 -StartServices
```

See [personal laptop setup](docs/PERSONAL-LAPTOP-SETUP.md), [model configuration/debugging](docs/MODELS-AND-DEBUGGING.md), and [MVP verification and remaining limitations](docs/MVP-STATUS.md).

## Three demonstrations

### Migration Studio

1. Open **Migration Studio** from Home.
2. Upload source data CSV, optional source metadata CSV, and required target metadata CSV, or click **Load synthetic payroll example**.
3. Inspect the schemas and source profile; click **Generate semantic mappings**.
4. Review direct, transformable, derived, ambiguous and missing fields. Edit source fields and transformation descriptions; resolve ambiguous choices or mark fields missing/rejected.
5. Confirm the review and click **Approve reviewed mapping plan**. The approved mapping is saved before execution.
6. Click **Generate and execute approved migration**. Generated SQL/Python uses the existing Docker sandbox.
7. Inspect actual target rows, field checks and invalid records. Download CSV, approved mapping, validation report, transformation source, notebook and connector ZIP.

Required metadata headers are `column_name,data_type,required,description`. Optional headers are `default_value,allowed_values,business_glossary_term,transformation_hint`. Types include scalar DuckDB types such as `VARCHAR`, `INTEGER`, `DOUBLE`, `DECIMAL(18,2)`, `DATE` and `BOOLEAN`. Allowed values accept a JSON array or a pipe-separated list. Provided source metadata must describe exactly the source columns; otherwise basic types and null observations are inferred without guessing business meaning.

The payroll example intentionally contains gaps, ambiguity and conversion errors. Successful computation can produce **MIGRATION_INCOMPLETE**: unresolved required values stay null and invalid rows are reported. That is an honest partial preview. Confidence labels are qualitative suggestions, not measured probabilities. Generic CSV exports do not establish Oracle HCM or other vendor import compatibility.

The connector ZIP contains metadata, approved mappings, transformation source, dependency/validation requirements and execution instructions. It is a reusable source package, not a live API connector or automatic connector-import feature. A new business context requires fresh review.

### Generic Data Workbench

Open **Data experimentation → Open Data Workbench**, upload CSV and describe a business problem. Review the profile and proposals, approve one, follow execution, and inspect persistent results and artifacts. The agent chooses an approach within available dependencies and resources; the application does not impose a fixed category list.

CSV profiling, feasibility checks, approval, bounded repairs, Docker execution, notebooks, saved-model prediction, dashboards and history are retained. Existing predictive and investigation examples remain available. Specifically labelled Codex-assisted examples use reviewed authored code and do not demonstrate autonomous Gemma generation.

Generic jobs run in separate local worker processes: closing the browser does not cancel them. Use **Cancel current run** in the monitor before stopping an active job. Migration actions currently run synchronously from their page; wait for completion before another operation on that workspace.

### External Docker application

Build the included trusted demonstration after building the sandbox:

```powershell
docker build --pull=false -f examples/external_app/Dockerfile -t workbench-launcher-demo:latest .
```

In **External applications**, register name `Customer Insights demo`, image `workbench-launcher-demo:latest`, internal port `8501`, and a description. Confirm trust, register, **Launch**, open the localhost link, then **Stop**.

Registration verifies a local image and pins its immutable ID. The launcher never pulls images. It records container IDs, verifies ownership labels and publishes an automatically assigned port on `127.0.0.1` only. Containers run as UID/GID 10001 with a read-only filesystem, writable `/tmp`, 2 GB RAM, 2 CPUs, and no host mounts, Docker sockets or privileged access. Image-declared volumes are unsupported.

Applications must listen on `0.0.0.0` inside the container and tolerate these limits. Stop removes the temporary container; persistent external-app storage is not provided. The default Docker bridge permits outbound traffic, unlike the network-disabled generated-code sandbox. Only trusted local images belong here. See the [complete MVP demo guide](docs/MVP-DEMO-GUIDE.md).

### Add your teammate's Docker image

Your teammate should provide a **Linux container image** with a working startup command (`CMD`/`ENTRYPOINT`), plus its internal HTTP port. The application must bind to `0.0.0.0` inside the container. For example, a Streamlit app can start with `streamlit run app.py --server.address 0.0.0.0 --server.port 8501`; a FastAPI app may use port 8000 instead.

On your teammate's computer, from their application folder:

```powershell
docker build -t teammate-app:v1 .
docker save --output teammate-app-v1.tar teammate-app:v1
```

Copy the TAR file to your laptop. With Docker Desktop running in Linux-container mode, load it into the **same Docker engine/context used by the workbench**:

```powershell
docker context show
docker load --input "$env:USERPROFILE\Downloads\teammate-app-v1.tar"
docker image inspect teammate-app:v1
```

Alternatively, if they share the source and Dockerfile, build locally with `docker build -t teammate-app:v1 C:\path\to\their-app`. If they supply a trusted registry image, pull it yourself first and register that exact local tag. The launcher does not download images.

In the workbench:

1. Open **Home → External Application → Register / Launch**, or choose **External applications** in the sidebar.
2. Enter the application name, image `teammate-app:v1`, its actual internal port (for example `8501`), and a description.
3. Confirm that you trust this local image, then register it.
4. Click **Launch** and open the displayed localhost URL. Use this assigned URL; the external app does not share the workbench's port 8501.
5. Click **Stop** when finished. This removes the application container and its temporary data while keeping the image and registration.

The image must support non-root UID/GID `10001:10001`, a read-only root filesystem and writes only to `/tmp`, within 2 GB RAM and 2 CPUs. It must not declare Docker volumes. This MVP does not configure custom environment variables, secrets, host mounts or persistent storage for external apps. Applications requiring those features need adaptation or separate hosting. Registration pins the image ID; to use a rebuilt version, give it a new tag and register a new version in the UI.

No workbench source-code change is needed to add a compatible teammate image.

### Stop after a demonstration

Stop external applications in their page, cancel any active workbench run, and press Ctrl+C in the Streamlit terminal. To release model RAM, run `ollama stop gemma3:4b` using your selected tag. Quit Ollama and Docker Desktop/Rancher Desktop from their tray menus when they are no longer needed. Do not delete workspaces if you want to retain results.

## Architecture and reused components

```text
Streamlit Home
  Migration Studio → mapping review → approved transformations
  Data Workbench → proposals → approval → generated solution
  External applications → trusted local Docker image lifecycle

Reasoning: Google ADK Runner + LlmAgent → local BaseLlm adapter → Ollama/Gemma
Workflow: Google ADK custom BaseAgent + deterministic Python operations
Execution: existing Docker sandbox → real tables/files → independent validation
Storage: existing workspace manager + DuckDB + local artifacts/stage history
```

`workbench/adk_agents.py` uses ADK's `BaseLlm` model extension point with the existing bounded local HTTP transports. It does not use LiteLLM or wrap LangGraph. ADK agents perform structured reasoning, and a custom ADK workflow agent drives the generic workflow. CSV ingestion, validation, artifact registration and Docker lifecycle remain deterministic Python.

Native model function calling is deliberately not required: the adapter rejects function calls and Python dispatches approved operations. Pydantic validates generated structures with bounded malformed-response retries. No silent cloud-provider fallback exists.

The existing workspace manager, CSV/DuckDB handling, query helpers, sandbox manager, artifact registry, notebook generation and results UI are reused. `workbench/migration.py` adds migration contracts and execution using them. `workbench/applications.py` adds the registry/launcher. Historical LangGraph checkpoint data is preserved; current execution writes ADK stage history and local events instead of running LangGraph.

## Configuration

Edit `.env` before starting. Process environment variables take precedence. Restart Streamlit after changes; active workers keep their starting settings.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_FRAMEWORK` | `google_adk` | Current orchestration |
| `LLM_PROVIDER` | `ollama` | Ollama or explicitly selected local `openai_compatible` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama service |
| `OLLAMA_MODEL` | `auto` | Discover installed Gemma, or pin an exact tag |
| `LLM_BASE_URL` | `http://localhost:1234/v1` | Optional local compatible server |
| `LLM_MODEL` | empty | Exact compatible-server model ID |
| `LLM_API_KEY` | empty | Optional local-server token |
| `LLM_STRUCTURED_MODE` | `json_schema` | Schema JSON or explicitly selected `json_object` |
| `LLM_TEMPERATURE` | `0.1` | Generation temperature |
| `LLM_TIMEOUT_SECONDS` | `180` | Per-request inference timeout |
| `LLM_CONTEXT_TOKENS` | `8192` | Context budget |
| `LLM_MAX_OUTPUT_TOKENS` | `4096` | Output budget; smaller than context |
| `WORKSPACE_ROOT` | `./workspaces` | Persistent workspace root |
| `MAX_UPLOAD_MB` | `100` | Source CSV upload limit |
| `SANDBOX_IMAGE` | `agentic-workbench-sandbox:latest` | Prebuilt analysis image |
| `SANDBOX_CPUS` | `4` | CPUs per execution |
| `SANDBOX_MEMORY` | `8g` | Memory per execution |
| `SANDBOX_TIMEOUT_SECONDS` | `300` | Execution deadline |
| `MAX_REPAIR_ATTEMPTS` | `2` | Maximum repairs after the initial attempt |

`auto` prefers smaller parameter sizes when sizes can be read from installed Gemma tags; it is not a hardware benchmark. Pin the selected tag for a repeatable demonstration. A longer timeout permits more waiting; it does not improve speed or guarantee correctness.

## Persistent data and source layout

```text
app.py                      Home and existing Streamlit pages
ui/migration.py             Migration upload, review, results and exports
ui/applications.py          External application controls
workbench/adk_agents.py      ADK model adapter, agents and workflow
workbench/llm.py             Local transports and structured validation
workbench/workflow.py        Generic workflow stages and durable state
workbench/migration.py       Migration contracts, execution and validation
workbench/applications.py    Persistent external application registry
sandbox/                    Existing Docker image and trusted runner
examples/migration/         Synthetic payroll data and metadata
examples/external_app/      Trusted launcher demo image source
scripts/                    Setup, startup, diagnostics and explicit demos
workspaces/                 Local data, models, results and logs
```

Migration workspaces preserve `input/`, `database/workspace.duckdb`, `generated/`, `notebooks/`, `results/`, `connector/`, `logs/` and `metadata/`. DuckDB tables include `source.data`, `source.metadata`, `target.metadata`, `mapping.proposals`, `mapping.approved`, `results.migrated_data` and `results.validation`. Original ingestion is preserved in the existing raw schema. File locks serialize writes.

The external application registry is under `workspaces/.applications/`. Environment files, `local-runtime.json`, virtual environments, workspaces and caches are ignored by Git. Folder relocation does not resolve or bypass endpoint-security detections; this project makes no sensor changes or security exclusions.

## Tests and verification status

**MVP handoff, 17 October 2026:** 290 automated tests passed, including real Docker integration tests, with zero failures/errors/skips. Live reviewed Gemma migration and external application launch/open/stop were demonstrated. The final live generic workbench code-generation run was cancelled during handoff; its complete live-model outcome remains unverified. See [the verification details and limitations](docs/MVP-STATUS.md). A small local model still needs review of proposed meanings, mappings and code.

Use the prepared interpreter or `.venv`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -m 'not docker and not live_ollama'
```

Actual Docker checks are separate and need a ready engine and built sandbox:

```powershell
$env:RUN_DOCKER_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest tests -q -m docker
Remove-Item Env:RUN_DOCKER_TESTS -ErrorAction SilentlyContinue
```

`scripts/check_model.py --generate --capabilities` makes a small real ADK/model request. The capabilities option reports Ollama's advertised capabilities; it does not claim a native tool-calling test. This implementation uses structured reasoning and deterministic approved operations. A small response is separate evidence from a complete live migration/workbench workflow.

Deterministic fixtures control model responses; opted-in Docker tests execute real computations. Fixture and assistant-authored demonstrations are labelled and must not be presented as autonomous live-model successes.

**Historical evidence:** `verification.json`, `showcase-verification.json`, `prediction-verification.json` and `live-workflow-verification.json` record earlier runs, not verification of this ADK/Migration Studio upgrade. Earlier Llama structured requests succeeded, but complete autonomous Llama workflows failed. Historical synthetic model results do not establish banking effectiveness. Current upgrade test counts and live Gemma outcomes must come from its actual new verification run.

## Troubleshooting and limits

| Symptom | Action |
|---|---|
| Model unavailable | Start Ollama, run `ollama list`, and select an installed Gemma. `auto` never downloads or substitutes another family. |
| Docker unavailable | Start one engine, inspect `docker context ls`, then run `docker info`. Rancher requires Moby/dockerd. |
| Sandbox image missing | Run `docker build -t agentic-workbench-sandbox:latest -f sandbox/Dockerfile .` here. |
| Slow or malformed response | Inspect the saved error, simplify the task, choose a suitable local model or deliberately adjust limits. Retries are bounded. |
| Incomplete migration | Inspect missing required fields and invalid records. Supply real information or legitimately revise target requirements; do not invent values. |
| External app exits | Check the internal port and compatibility with UID 10001, read-only filesystem and resource limits. |
| Runtime missing after relocation | Create `.venv` with setup or supply `-PythonExe`. The local runtime pointer is machine-specific. |
| Workspace busy | Wait or cancel through supported controls. Do not remove locks to force concurrent writes. |

Generated code has no sandbox network access and cannot install packages. A GPU/deep-learning runtime is not included by default. Structural checks do not establish business correctness. The UI binds to loopback and has no multi-user authorization. External apps have no persistent volume management, and connector packages have no live external-system integration. Cloud deployment is outside this local MVP.

See [model configuration and reviewed-code debugging](docs/MODELS-AND-DEBUGGING.md) for detailed troubleshooting.
