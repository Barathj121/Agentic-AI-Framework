# Local models, ADK, Docker and debugging

The current runtime is Google ADK with local HTTP model transports. Gemma through Ollama is the primary configuration. The existing local OpenAI-compatible provider is retained for an already running compatible server. Neither path needs a cloud API key.

## Configure Gemma through Ollama

Start Ollama and inspect actual installed tags:

```powershell
ollama list
```

Use these entries in `.env`, replacing existing values rather than adding duplicates:

```dotenv
AGENT_FRAMEWORK=google_adk
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=auto
LLM_TEMPERATURE=0.1
LLM_TIMEOUT_SECONDS=180
LLM_CONTEXT_TOKENS=8192
LLM_MAX_OUTPUT_TOKENS=4096
LLM_STRUCTURED_MODE=json_schema
```

`auto` discovers installed Gemma tags and prefers smaller parameter sizes when a size is recognizable in the tag. It does not download models, fall back to Llama, or call a cloud provider. If none is installed, download a selected Gemma variant first. For example, `gemma3:4b` is an illustrative tag, not a hardcoded requirement:

```powershell
# Example only: use a Gemma variant you selected for this machine.
ollama pull gemma3:4b
ollama list
```

For repeatable demos, set `OLLAMA_MODEL` to the exact installed tag. The model's name or a label such as “128 QAT” alone does not establish memory needs, quality or the served API ID. A 32 GB CPU-only machine still needs RAM for Windows, Docker and analysis workloads in addition to the LLM. Larger models and contexts need more memory; longer timeouts do not make generation faster.

On a fresh installation, check:

```powershell
.\.venv\Scripts\python.exe scripts\doctor.py
.\.venv\Scripts\python.exe scripts\check_model.py
.\.venv\Scripts\python.exe scripts\check_model.py --generate --capabilities
```

On this relocated machine without a local `.venv`, first set:

```powershell
Set-Location 'C:\Users\bkumarj\Desktop\Agentic-Workbench'
$workbenchPython = (Get-Content -LiteralPath '.\local-runtime.json' -Raw | ConvertFrom-Json).python_executable
& $workbenchPython scripts\doctor.py
& $workbenchPython scripts\check_model.py --generate --capabilities
```

The doctor checks availability without inference. `check_model.py` without flags also checks availability. `--generate` performs an actual small ADK invocation and validates its JSON against a Pydantic schema. `--capabilities` queries the selected Ollama model's advertised capabilities. This is not a native function-call success test or proof that a full workflow will complete.

## How the ADK integration works

`workbench/adk_agents.py` provides a `LocalModel(BaseLlm)` adapter, `LlmAgent` reasoning instances and ADK `Runner` invocations. The adapter forwards structured generation through the existing local transport. The generic data workflow is driven by a custom ADK `BaseAgent`; ordinary ingestion, execution, validation and routing remain deterministic Python.

This uses ADK's model extension point rather than LiteLLM. There is no LangGraph execution hidden inside it. Current state and stage history are persisted by the workbench; ADK's in-memory reasoning session is not the sole durable record. Historical LangGraph checkpoint tables are retained but are not used as the active workflow engine.

Logical responsibilities include data understanding, planning, migration mapping, solution building and validation. Not every responsibility requires a separate model invocation: direct mapping SQL, schema checks, exports, lifecycle operations and actual output checks are deterministic when possible.

Native tool calling is deliberately disabled for the local adapter. It rejects function calls; Python invokes only approved operations. This avoids depending on a Gemma variant supporting reliable native tools. It does not make structured generation infallible: every response is Pydantic-validated, malformed responses have limited retries, and persistent failures are reported.

## Use a model served by LM Studio

If your existing application exposes a local OpenAI-compatible API, start that server and query its actual model IDs. Do not infer an API address from the application's name:

```powershell
(Invoke-RestMethod -Uri 'http://localhost:1234/v1/models').data | Select-Object id
```

Replace the example address with the one shown by your server and configure:

```dotenv
AGENT_FRAMEWORK=google_adk
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://localhost:1234/v1
LLM_MODEL=<exact model ID returned by the server>
LLM_API_KEY=
LLM_STRUCTURED_MODE=json_schema
LLM_TIMEOUT_SECONDS=180
LLM_CONTEXT_TOKENS=8192
LLM_MAX_OUTPUT_TOKENS=4096
```

`openai_compatible` describes a local API format; it does not select an OpenAI service. The adapter accepts loopback/private literal addresses and rejects public cloud endpoints and redirects. Use the local server's token only if that server requires authentication, and keep it in the ignored `.env` or the current process environment. Never place it in source or documentation.

Load the model with a server context at least as large as the configured context. For this provider the client budget does not change the server's loaded context. If the server explicitly rejects JSON Schema but supports JSON object mode, deliberately select `LLM_STRUCTURED_MODE=json_object` and retest. Pydantic validation still applies; the application does not silently change modes.

Finish or cancel jobs before changing providers/settings, then restart Streamlit. Existing workers retain their original settings. Changing a model or endpoint needs no source edit and no sandbox image rebuild.

## Choose Docker Desktop or Rancher Desktop

Use one running engine for this project:

| Engine | Required configuration |
|---|---|
| Docker Desktop | WSL2 backend and Linux containers |
| Rancher Desktop | Moby/dockerd engine |

```powershell
docker context ls
docker context show
docker info
docker info --format '{{.OSType}}'
```

The operating-system result must be `linux`. To select an installed context, use its exact listed name:

```powershell
docker context use '<exact context name>'
docker info
```

Images belong to the selected engine. If the sandbox image is missing there, build it from this repository:

```powershell
docker build -t agentic-workbench-sandbox:latest -f sandbox/Dockerfile .
```

The startup script adds a detected Rancher/Docker CLI directory and the usual Ollama directory to its process PATH. For manual commands in a different terminal where Docker is not found, use the installed executable's full path or add its directory to that terminal's PATH. No machine-wide PATH change is required.

## Edit and debug the code

Open the same source checkout in your editor. On this prepared machine it is `C:\Users\bkumarj\Desktop\Agentic-Workbench`. The image contains approved libraries and the trusted execution runner, while application source and generated solutions remain editable files.

| Change | Source |
|---|---|
| Model, provider, limits | `.env` |
| Home/navigation and generic screens | `app.py` |
| Migration screens | `ui/migration.py` |
| Application launcher screens | `ui/applications.py` |
| ADK adapters and runtime | `workbench/adk_agents.py` |
| Local transport/response validation | `workbench/llm.py` |
| Generic agent prompts and stages | `workbench/prompts.py`, `workbench/workflow.py` |
| Migration mappings and execution | `workbench/migration.py` |
| Application registry/lifecycle | `workbench/applications.py` |
| Workspace/CSV/SQL operations | `workbench/workspace.py`, `data.py`, `query.py` |
| Sandbox and actual output validation | `workbench/sandbox.py`, `artifacts.py` |
| Analysis runtime packages/runner | `sandbox/requirements.txt`, `Dockerfile`, `runner.py` |

Restart Streamlit after host code changes; active workers do not adopt edits. Install changed host requirements into the selected virtual environment. Rebuild the sandbox after changing its Dockerfile, packages or trusted runner. Changing an external application's image needs a separate image build and a new registration, because registrations pin image IDs.

### Find a failed run's evidence

| Workspace path | Evidence |
|---|---|
| `metadata/workspace.json` | Status and latest error |
| `metadata/state.json`, `metadata/checkpoints.sqlite` | Generic workflow state and ADK stage history |
| `metadata/selected_plan.json` | Approved generic solution |
| `generated/mapping.json` | Proposed migration mapping |
| `metadata/mapping_approval.json` | Immutable approved migration plan and fit-gap |
| `generated/approved_mapping.json` | Approved mapping export |
| `metadata/build.json`, `generated/` | Execution contract and generated source |
| `metadata/execution.json`, `metadata/validation.json` | Actual execution and registered-output checks |
| `results/validation.json`, `metadata/migration_result.json` | Migration field/record checks |
| `logs/agent.jsonl` | Application activity |
| `logs/adk-reasoning.jsonl`, `logs/adk-workflow.jsonl` | ADK events when produced |
| `logs/worker.log`, `logs/attempt-*.log` | Worker and container execution output |
| `database/workspace.duckdb` | Materialized data and validation tables |

Inspect actual evidence before changing prompts. Model/API failure, generated-code failure, and an incomplete migration due to missing source information are different problems. Do not modify a workspace database during execution or remove locks to force access.

### Review and rerun edited generic solution code

For an existing generic workspace with an approved plan and saved build, edit its files under `generated/` while retaining filenames, entrypoint and approved output contract. Inspect the changed snapshot first:

```powershell
.\.venv\Scripts\python.exe scripts\debug_solution.py --workspace 'uc_...'
```

After reviewing it, explicitly authorize one isolated rerun:

```powershell
.\.venv\Scripts\python.exe scripts\debug_solution.py --workspace 'uc_...' --execute-reviewed-code
```

This creates a new DEBUG workspace with the original CSV and reviewed source, executes once in Docker, and records provenance. It makes no model calls and leaves the original history intact. This is the existing generic-workbench debugging path, not a migration-connector importer. To revise a Migration Studio approval, open the same saved migration and choose **Edit approved mappings**. The uploaded source stays in place; previous approvals and outputs are archived, and current execution/export approval is revoked until you review and approve the revised plan. **Previous mapping approvals** shows the saved historical decisions. If reopening is interrupted, choose **Resume mapping review** before approving or executing again.

Do not run generated modules/notebook cells or deserialize generated models directly on the host to bypass the sandbox.

## Downloads and proxy troubleshooting

The workbench's local HTTP client ignores ambient proxy variables for local inference. This does not alter Ollama's own model-download environment or Windows proxy settings. A proxy-related model download failure must be addressed in the Ollama server's network/session configuration; the error is not proof of an application defect. Use the network route and proxy configuration approved for that machine.

A successful older Llama inference or a reviewed fixture demonstration is not current Gemma verification. Run the actual configured model check, then the full workflow you intend to demonstrate. Preserve its logs, approved inputs and outputs.
