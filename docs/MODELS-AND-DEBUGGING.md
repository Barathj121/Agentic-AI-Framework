# Local models, Docker and debugging

You can use an existing model in Bionic/LM Studio with this workbench. Bionic is the coding/work agent; the workbench calls the underlying local model API. Opening the model in Bionic does not by itself configure this application's endpoint or model ID. Codex is not required. [LM Studio Bionic](https://lmstudio.ai/)

## Use a model served by LM Studio

1. In **Bionic**, select your downloaded model and open **Settings → Local Model API**. Enable the local API server and copy its displayed base URL. In the classic **LM Studio** interface, use **Developer → Start server** instead. These are alternative interfaces; you do not need a second model application if your existing Bionic installation already provides the API. If the `lms` command is available, `lms server start` also starts the server. [Bionic API instructions](https://github.com/meta-models/meta-oss-cookbook/blob/main/inference-server/lm-studio.md), [LM Studio server documentation](https://lmstudio.ai/docs/developer/core/server)
2. Keep the intended model loaded. Query the server for its exact identifier. The usual base is `http://localhost:1234/v1`; use the address shown by your installation if different:

   ```powershell
   (Invoke-RestMethod -Uri "http://localhost:1234/v1/models").data |
       Select-Object id
   ```

   The compatibility API exposes `/v1/models` and `/v1/chat/completions`. Copy the returned `id` exactly, including any organization or quantization suffix. [LM Studio compatibility API](https://lmstudio.ai/docs/developer/openai-compat)
3. In the repository's `.env`, set the following values. Replace the placeholder with the actual returned ID:

   ```dotenv
   LLM_PROVIDER=openai_compatible
   LLM_BASE_URL=http://localhost:1234/v1
   LLM_MODEL=<exact ID returned by /v1/models>
   LLM_API_KEY=
   LLM_STRUCTURED_MODE=json_schema
   LLM_TIMEOUT_SECONDS=600
   LLM_CONTEXT_TOKENS=8192
   LLM_MAX_OUTPUT_TOKENS=4096
   ```

   Replace existing entries and add only missing keys; avoid duplicates. Leave `OLLAMA_*` values alone; they are used only when `LLM_PROVIDER=ollama`. `openai_compatible` describes the local API format and does not send requests to OpenAI or require an OpenAI account. This implementation accepts loopback addresses and literal private LAN IP addresses; cloud endpoints and redirects are rejected.
4. Load the model with a server context of at least `8192` tokens for this configuration. `LLM_CONTEXT_TOKENS` controls client prompt budgeting for this provider; it does not change the model's loaded context. A model advertised as supporting 128K is not necessarily running with that context. Keep `LLM_MAX_OUTPUT_TOKENS` smaller than the context budget. Larger settings also need more memory.
5. From the project directory, verify the services and make one real structured-generation request:

   ```powershell
   .\.venv\Scripts\python.exe scripts\doctor.py
   .\.venv\Scripts\python.exe scripts\check_model.py --generate
   .\scripts\start.ps1
   ```

   `doctor.py` checks availability without inference. `check_model.py --generate` tests the configured adapter's actual structured response. These checks do not prove that a complete analytical workflow will succeed.

### Gemma names, smaller models and JSON output

Use the ID your server returns for the Gemma model you actually downloaded. A label such as “128QAT” is not enough to determine a model version, parameter size or API ID; do not convert it into an assumed model name. The same configuration works for another served model by changing `LLM_MODEL` and loading that model in the server. No Python source edit or Docker rebuild is needed.

The workbench requests schema-constrained JSON by default. If your server explicitly rejects that capability but supports JSON object mode, deliberately set `LLM_STRUCTURED_MODE=json_object` and repeat the generation check. The application still validates every response against its full schema, and does not switch modes or providers silently. LM Studio documents structured-output support and notes that model capability varies. [Structured output documentation](https://lmstudio.ai/docs/developer/openai-compat/structured-output)

A smaller model may run faster or fit available memory but can also produce weaker proposals or invalid code. Compare candidates on the same input and approved objective: record time, proposal quality, successful output validation and useful results. There is no project evidence that a particular Gemma model is universally better than the default Llama model. The live smoke test provides the same six-row dataset and expected total for a repeatable starting point:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_test.py --real-model
# Review the printed proposals, then use the exact IDs printed by the command:
.\.venv\Scripts\python.exe scripts\smoke_test.py --real-model --workspace "uc_..." --approve "proposal_..."
```

For CPU inference, `LLM_TIMEOUT_SECONDS=600` is a suggested starting allowance per request, not a completion guarantee. In the tested CPU-only `llama3.1:8b` workflow, the initial generated script contained a syntax error and the repair request exceeded 600 seconds; that live workflow did not complete. Increasing the timeout allows more waiting but does not make inference faster. Choose a model and quantization that fit your machine, and evaluate them with the same smoke test. The sandbox's CPU and memory settings govern generated analysis code, separately from the local model server.

### Optional local server authentication

Leave `LLM_API_KEY` empty if local server authentication is disabled. If it is enabled, use that server's token. To supply it only to the current PowerShell session without typing it in command history:

```powershell
$modelSecret = Read-Host "Local model server token" -AsSecureString
$env:LLM_API_KEY = [System.Net.NetworkCredential]::new("", $modelSecret).Password
$modelHeaders = @{ Authorization = "Bearer $env:LLM_API_KEY" }
(Invoke-RestMethod -Uri "http://localhost:1234/v1/models" -Headers $modelHeaders).data |
    Select-Object id
.\scripts\start.ps1
```

Start the workbench from that same shell so it inherits the token. Process environment variables override `.env`. Alternatively, keep the token in your local `.env`, which Git ignores; do not paste it into logs, documentation or commits. The adapter sends a Bearer header only when a key is configured.

## Use an Ollama model instead

Keep Ollama running, list downloaded models, and use the exact tag:

```powershell
ollama list
# Download the project's default example only if needed:
ollama pull llama3.1:8b
```

```dotenv
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b
LLM_TIMEOUT_SECONDS=600
```

Change `OLLAMA_MODEL` to another downloaded tag if desired. `LLM_MODEL` is used by the compatible provider, not Ollama. For both providers, wait for or cancel active jobs before changing configuration, then restart Streamlit. Existing background workers retain the settings they started with. The [README's proxy instructions](../README.md#ollama-download-and-a-stale-proxy) explain temporary Ollama download settings.

## Choose Docker Desktop or Rancher Desktop

Use one running engine for this project at a time:

| Engine | Required setting |
|---|---|
| Docker Desktop | WSL 2 backend and Linux containers. See [Docker's WSL instructions](https://docs.docker.com/desktop/features/wsl/). |
| Rancher Desktop | **Moby/dockerd** container engine. This provides the Docker API used by the application; the containerd option uses a different interface. See [Rancher's engine settings](https://docs.rancherdesktop.io/ui/preferences/container-engine/general/). |

Check which engine the Docker CLI currently targets:

```powershell
docker context ls
docker context show
docker info
docker info --format '{{.OSType}}'
```

The operating-system result must be `linux`. If the selected context points to the wrong engine, choose the relevant name shown by `docker context ls`:

```powershell
docker context use "<exact context name from the list>"
docker info
```

Images are stored by the selected engine. After switching engines, build the sandbox there if it is missing:

```powershell
docker build -t agentic-workbench-sandbox:latest -f sandbox/Dockerfile .
.\.venv\Scripts\python.exe scripts\doctor.py
```

Match the tag to `SANDBOX_IMAGE` if you changed that setting. Starting Docker Desktop does not require changes to application Python code, and changing the model does not require rebuilding the sandbox.

## Edit and debug the code

Open the **same `Agentic-AI-Framework` checkout** as a local project in Bionic or your preferred editor. Bionic can work against a local project folder; enable its coding access if you want it to edit files and run development commands. [Bionic local project instructions](https://github.com/meta-models/meta-oss-cookbook/blob/main/inference-server/lm-studio.md)

The application remains ordinary editable Python source. The Docker image contains Python, approved analysis libraries and the trusted runner. It does not freeze the whole application into an opaque image: the host app supplies each workspace's generated source to the execution container as read-only files.

| What you want to change | File or folder |
|---|---|
| Provider, model, timeout or resource limits | `.env` |
| Streamlit screens and interaction | `app.py`, `ui/helpers.py` |
| Environment settings | `workbench/settings.py` |
| Local model requests and structured responses | `workbench/llm.py` |
| Agent instructions | `workbench/prompts.py` |
| Proposal, approval, generation and repair workflow | `workbench/workflow.py` |
| Background jobs and UI-facing operations | `workbench/service.py`, `workbench/worker.py` |
| CSV profiling and bounded queries | `workbench/data.py`, `workbench/query.py` |
| Docker execution and output validation | `workbench/sandbox.py`, `workbench/artifacts.py` |
| Trusted container runner or installed analysis packages | `sandbox/runner.py`, `sandbox/requirements.txt`, `sandbox/Dockerfile` |
| A particular generated solution | `workspaces/<workspace ID>/generated/` |

Stop or cancel active jobs before changing their code. After editing application Python files, restart Streamlit with `scripts\start.ps1`; a running background worker will not adopt your edits. For host dependency changes, run `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` before restarting. Rebuild the image only when changing its `sandbox/Dockerfile`, `sandbox/requirements.txt` or `sandbox/runner.py`.

### Find a failed run's evidence

Use the workspace ID from the UI's history or command output. The default root is `workspaces`; `WORKSPACE_ROOT` can override it, and `doctor.py` prints the resolved location.

| Relative to the workspace | Evidence |
|---|---|
| `metadata/workspace.json` | Persisted status and error |
| `metadata/selected_plan.json` | Approved proposal and additional instructions |
| `metadata/build.json` | Saved generated files, entrypoint and output contract |
| `generated/solution.py` | Fixed required entrypoint for every generated solution; both workflow validation and the sandbox enforce this path |
| `metadata/execution.json`, `metadata/validation.json` | Latest execution and real output checks |
| `metadata/attempts/<number>/` | Per-attempt build, execution, validation or generation-error evidence as produced |
| `metadata/manifest.json` | Registered outputs and execution summary |
| `logs/agent.jsonl` | Persisted progress events |
| `logs/worker.log` | Worker process output and traceback |
| `logs/attempt-0.stdout.log`, `logs/attempt-0.stderr.log` | First execution's stdout/stderr; subsequent attempts use increasing zero-based numbers |
| `database/workspace.duckdb` | Workspace data and persistent audit tables; do not edit during active execution |

Inspect the actual failure and generated source before changing prompts. A failed model request, failed Docker process and failed output validation need different fixes. Preserve earlier evidence; it makes a proposed fix reviewable.

### Review and rerun edited generated code

For a workspace that already has an approved plan and a saved build, edit its existing files under `generated/`. Keep the saved filenames, entrypoint, validation checks and approved output contract. This debugging path does not add arbitrary files or approve a different analysis. A changed objective or output schema needs new proposals and approval through the normal workflow.

First inspect the edited snapshot without executing it:

```powershell
.\.venv\Scripts\python.exe scripts\debug_solution.py --workspace "uc_..."
```

The JSON report lists file hashes, changes from the saved build and expected outputs, after checking syntax and the saved contract. It does not run Docker or call a model. After reviewing the code, explicitly authorize one sandbox run:

```powershell
.\.venv\Scripts\python.exe scripts\debug_solution.py --workspace "uc_..." --execute-reviewed-code
```

This command creates a **new `DEBUG` workspace**, re-ingests the original CSV, snapshots the edited source and executes it once through Docker and the normal output validation. It makes no model calls and performs no automatic repair. The original execution history, checkpoints and results remain in the source workspace. The new `metadata/debug_provenance.json` records the source workspace, hashes and explicit approval. Open the returned new workspace ID in the UI to inspect the result; `COMPLETED` means its output checks passed. An active source job or unresolved container cleanup blocks debugging. Do not run generated Python, notebook cells or model pickle files directly on the host to bypass the sandbox.

You can give Bionic this task in the same checkout:

> Read README.md and docs/MODELS-AND-DEBUGGING.md. Investigate workspace `<ID>` using its actual status, agent events, execution logs, validation report and generated source. Explain the observed failure, then make the smallest relevant fix. Preserve the explicit approval gate, approved outputs and Docker isolation. If editing generated code, inspect it with scripts/debug_solution.py first and show the review; run the explicit --execute-reviewed-code command only when I approve that snapshot. If editing application code, run the relevant existing tests. Report commands, actual results and any remaining failure. Do not run generated modules or deserialize model artifacts on the host.

The regular development check is `.\.venv\Scripts\python.exe -m pytest tests -v`; Docker tests require the opt-in instructions in the [README](../README.md#tests-and-verification-status). Model/API checks, deterministic tests, fixture demonstrations and a successful live end-to-end workflow are separate pieces of evidence.
