# Run on a personal Windows laptop

Codex is not needed to run or edit this project. Use Python, a local Gemma model through Ollama, and Docker Desktop/WSL2 or Rancher Desktop/Moby. The application remains editable source; only generated analysis runs are sent to the sandbox.

## 1. Get the current source and prerequisites

Install Git if cloning, Python 3.11 or newer, Ollama, and one Linux container engine. Start the engine before building images. The target machine has 32 GB RAM and a CPU; there is no mandatory GPU.

The upgrade is local until explicitly published. Copy the current source directory, or clone a revision confirmed to contain `workbench/adk_agents.py`, `workbench/migration.py`, `ui/migration.py` and `examples/migration/`. A clone of an older GitHub revision will not acquire unpushed local changes.

When the intended revision is available on GitHub:

```powershell
Set-Location ([Environment]::GetFolderPath('Desktop'))
git clone https://github.com/barathj121/Agentic-AI-Framework.git Agentic-Workbench
Set-Location .\Agentic-Workbench
```

If you copied the folder instead, open PowerShell there. Do not copy another machine's virtual environment or rely on its `local-runtime.json`; setup creates a fresh `.venv`. Copy saved workspaces separately only if you want their local datasets/results.

Check prerequisites:

```powershell
py -3.11 --version
docker context ls
docker info
docker info --format '{{.OSType}}'
```

Docker must report `linux`. If its daemon is unavailable, start Docker Desktop with WSL2/Linux containers, or Rancher Desktop with Moby/dockerd. If Windows reports WSL/virtualization requirements, complete the engine's documented installation before continuing.

If `docker` is not on this terminal's PATH, add your installed engine's CLI directory for this session before setup. For Docker Desktop:

```powershell
$env:Path = "$env:ProgramFiles\Docker\Docker\resources\bin;$env:Path"
```

For Rancher Desktop instead:

```powershell
$env:Path = "$env:ProgramFiles\Rancher Desktop\resources\resources\win32\bin;$env:Path"
```

## 2. Install dependencies and build the sandbox

```powershell
.\scripts\setup.ps1 -BuildSandbox
```

This creates `.venv`, installs the host dependencies (including Google ADK), preserves an existing `.env`, generates the existing data examples, and builds the reusable analysis image.

If the default Python launcher does not find your interpreter:

```powershell
.\scripts\setup.ps1 -PythonExe 'C:\path\to\python.exe' -BuildSandbox
```

If your environment does not allow local PowerShell scripts, follow its approved script policy or use ordinary manual commands:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
docker build -t agentic-workbench-sandbox:latest -f sandbox/Dockerfile .
.\.venv\Scripts\python.exe examples\generate_data.py
```

## 3. Select an installed Gemma model

Start Ollama and inspect actual installed models:

```powershell
ollama list
```

If no Gemma is available, select and download a suitable variant. This is an example, not a mandatory or assumed installed tag:

```powershell
ollama pull gemma3:4b
ollama list
```

Set `.env`:

```dotenv
AGENT_FRAMEWORK=google_adk
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=auto
LLM_TEMPERATURE=0.1
LLM_TIMEOUT_SECONDS=180
LLM_CONTEXT_TOKENS=8192
LLM_MAX_OUTPUT_TOKENS=4096
```

`auto` chooses an installed Gemma. It is not a tag for `ollama pull`, and it never falls back to a cloud service. Pin the exact installed tag instead for repeatable demos. CPU requests may need a longer timeout; this does not guarantee stronger reasoning or faster execution.

If your existing local model application provides an OpenAI-compatible API, the retained alternative is documented in [MODELS-AND-DEBUGGING.md](MODELS-AND-DEBUGGING.md#use-a-model-served-by-lm-studio). Use its actual model ID and local URL; the application name alone is insufficient.

## 4. Verify and start

```powershell
.\.venv\Scripts\python.exe scripts\doctor.py
.\.venv\Scripts\python.exe scripts\check_model.py --generate --capabilities
.\scripts\start.ps1 -StartServices
```

The doctor checks the model, engine and image. The second command performs a small real ADK structured-generation request and reports Ollama capabilities. It does not establish complete workflow success or native function calling. The implementation uses structured reasoning plus deterministic approved operations.

Open [http://127.0.0.1:8501](http://127.0.0.1:8501). Keep PowerShell open. The startup script can launch installed services, but allow the engine time to become ready.

If both engines are installed, start the intended one first; automatic startup otherwise checks Rancher Desktop before Docker Desktop. Select its matching Docker context.

For manual application startup:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

Later starts reuse the model, packages and images. Start services and rerun `scripts/start.ps1`; do not repeat setup routinely.

## 5. Demonstrate all three workflows

Follow [MVP-DEMO-GUIDE.md](MVP-DEMO-GUIDE.md):

1. Migration Studio: load synthetic payroll, generate mappings, review/approve, execute, inspect gaps and export.
2. Data Workbench: upload CSV, describe a problem, approve a proposal, execute and inspect actual persisted outputs.
3. External application: register an existing trusted image, launch, open the local URL and stop.

To build the included external demo without additional packages:

```powershell
docker build --pull=false -f examples/external_app/Dockerfile -t workbench-launcher-demo:latest .
```

Register `workbench-launcher-demo:latest` with internal port `8501`.

## 6. Develop and stop

Edit this same checkout in an editor. Host code changes normally need an application restart; sandbox runner/package changes need an image rebuild. [The debugging guide](MODELS-AND-DEBUGGING.md#edit-and-debug-the-code) maps relevant files and safe reruns.

Cancel active generic workbench jobs through the monitor, wait for migration actions to finish, and stop external applications through their Stop buttons. Then Ctrl+C stops Streamlit. Quit Ollama and the selected container engine when you want to release their resources. Saved inputs, model weights and workspace results remain on disk.

No cloud account, Gemini key, Kubernetes or separate API backend is part of this setup.
