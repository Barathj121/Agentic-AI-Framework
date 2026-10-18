# Agentic Data & Application Workbench

A local Python application for building, executing, and inspecting data-driven solutions. Its home page brings together **Migration Studio**, the **Data Workbench**, and a launcher for **trusted local Docker applications**. 

Google ADK runs the core workbench's agent execution and structured reasoning. Gemma is served locally through Ollama; Streamlit provides the UI, DuckDB stores workspace data, and Docker executes generated code. Core workflows require no paid inference service, cloud deployment, or separate backend. Results and exports come from executed code, not conversational answers.

---

## Start the Desktop Checkout

Open PowerShell:

```powershell
Set-Location 'C:\Users\bkumarj\Desktop\Agentic-Workbench'
.\scripts\setup.ps1 -BuildSandbox  # First run, or after removing the old Python environment
.\scripts\start.ps1 -StartServices
```

Open [http://127.0.0.1:8501](http://127.0.0.1:8501). Keep the terminal open; `Ctrl+C` stops Streamlit. 

* **Docker Engine:** This starts installed Ollama and a Docker desktop engine if needed (supports Rancher Desktop with Moby/dockerd or Docker Desktop with WSL2).
* **Interpreter:** A repository-local `.venv` is created automatically. An explicit interpreter can also be passed via `.\scripts\start.ps1 -PythonExe 'C:\path\to\python.exe' -StartServices`.

For checks using the repository's environment:

```powershell
$workbenchPython = '.\.venv\Scripts\python.exe'
& $workbenchPython scripts\doctor.py
& $workbenchPython scripts\check_model.py --generate --capabilities
```

---

## Set up Another Windows Computer

Requirements: Python 3.11 or newer, Ollama, and a Linux container engine (Docker Desktop with WSL2 or Rancher Desktop with Moby/dockerd). Recommended setup includes 32 GB RAM and a CPU (GPU optional).

```powershell
git clone [https://github.com/Barathj121/Agentic-AI-Framework.git](https://github.com/Barathj121/Agentic-AI-Framework.git)
Set-Location Agentic-AI-Framework
.\scripts\setup.ps1 -BuildSandbox
ollama pull gemma3:4b
ollama list
```

Configure your `.env` file for the local CPU demonstration:

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

Run diagnostics and start services:

```powershell
.\.venv\Scripts\python.exe scripts\doctor.py
.\.venv\Scripts\python.exe scripts\check_model.py --generate --capabilities
.\scripts\start.ps1 -StartServices
```

---

## Three Core Demonstrations

### 1. Migration Studio
1. Open **Migration Studio** from Home.
2. Upload a source data CSV, optional source metadata CSV, and required target metadata CSV, or click **Load synthetic payroll example**.
3. Inspect schemas and profiles, then click **Generate semantic mappings**.
4. Review fields (direct, transformable, derived, ambiguous, and missing). Edit descriptions and resolve choices.
5. Click **Approve reviewed mapping plan**, followed by **Generate and execute approved migration**.
6. Inspect target rows, checks, and invalid records. Download CSV, mapping files, validation reports, and connector ZIPs.

### 2. Generic Data Workbench
1. Open **Data experimentation → Open Data Workbench**.
2. Upload a CSV file and describe your business problem.
3. Review profile proposals, approve one, monitor execution, and inspect persistent results and generated notebooks.

### 3. External Docker Application Launcher
Build and run a trusted local container image:
```powershell
docker build --pull=false -f examples/external_app/Dockerfile -t workbench-launcher-demo:latest .
```
In **External applications**, register the name, image tag (`workbench-launcher-demo:latest`), and internal port (`8501`). Confirm trust, click **Register**, then **Launch** to access via localhost. 

Containers run securely with isolated constraints (non-root UID/GID 10001, read-only root filesystem, writable `/tmp`, 2 GB RAM, 2 CPUs, no host mounts).

---

## Architecture

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

---

## Configuration Reference

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_FRAMEWORK` | `google_adk` | Current orchestration framework |
| `LLM_PROVIDER` | `ollama` | Ollama or local `openai_compatible` server |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama service endpoint |
| `OLLAMA_MODEL` | `auto` | Discover installed Gemma, or pin an exact tag |
| `LLM_TEMPERATURE` | `0.1` | Generation temperature |
| `WORKSPACE_ROOT` | `./workspaces` | Persistent workspace root path |
| `SANDBOX_IMAGE` | `agentic-workbench-sandbox:latest` | Prebuilt analysis image name |
| `SANDBOX_CPUS` | `4` | CPU core limit per execution |
| `SANDBOX_MEMORY` | `8g` | Memory limit per execution |

---

## Troubleshooting & Quick Fixes

* **Model unavailable:** Ensure Ollama is running (`ollama list`) and your `.env` specifies an installed model tag (e.g., `gemma3:4b`). `auto` will not download models automatically.
* **Docker unavailable:** Verify your container runtime engine is active and context is configured properly (`docker context ls`).
* **Sandbox image missing:** Build it manually via:
  ```powershell
  docker build -t agentic-workbench-sandbox:latest -f sandbox/Dockerfile .
  ```
* **External app exits:** Check that the container listens on `0.0.0.0` inside the container and adheres to UID `10001` read-only root filesystem boundaries.
```
