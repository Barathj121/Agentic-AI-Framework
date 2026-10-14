# Run on your personal Windows laptop

**Codex is not required.** The application runs with Python, a local model API and a Docker-compatible Linux container engine. GitHub contains the editable application source and synthetic examples; model weights and the sandbox image are downloaded or built separately.

## 1. Install the prerequisites

- [Git for Windows](https://git-scm.com/install/windows).
- [Python 3.11 for Windows](https://www.python.org/downloads/windows/), including the Python launcher. The setup script uses `py -3.11` by default.
- **One model server:** use your existing [Bionic/LM Studio](https://lmstudio.ai/) model through its local API, or install [Ollama for Windows](https://ollama.com/download/windows). You do not need Ollama if your Bionic API is configured.
- **One container engine:** [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/) with its **WSL 2 backend and Linux containers**, or [Rancher Desktop](https://docs.rancherdesktop.io/getting-started/installation/) with **Moby/dockerd** selected.

If WSL is missing, open PowerShell **as administrator**, run `wsl --install`, and restart Windows when requested. [Microsoft WSL installation instructions](https://learn.microsoft.com/en-us/windows/wsl/install)

Start your chosen container engine and wait until it is ready. Use one engine at a time. If both are installed, the [model and debugging guide](MODELS-AND-DEBUGGING.md#choose-docker-desktop-or-rancher-desktop) explains how to select the correct Docker context.

## 2. Download the source and build the sandbox

Open a **new, ordinary PowerShell window** in the folder where you want the project:

```powershell
git clone https://github.com/barathj121/Agentic-AI-Framework.git
cd Agentic-AI-Framework
py -3.11 --version
docker context ls
docker info
docker info --format '{{.OSType}}'
```

The Docker operating-system result should be `linux`. If `docker info` fails, start or select the intended engine before continuing. If GitHub requests authentication, sign in to an account with repository access. Once these checks pass, run setup:

```powershell
.\scripts\setup.ps1 -BuildSandbox
```

Setup creates `.venv`, installs Python packages, creates `.env` if absent, generates sample CSVs and builds the sandbox image.

If PowerShell blocks the script, allow it for this process only, then repeat setup:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\setup.ps1 -BuildSandbox
```

If `py -3.11` cannot find your installed interpreter, use `.\scripts\setup.ps1 -PythonExe "C:\path\to\python.exe" -BuildSandbox`. The override must be Python 3.11 or newer; the development host was tested with Python 3.12.

## 3. Configure one local model

**Existing Bionic/LM Studio model:** In Bionic, open **Settings → Local Model API**, enable the server and copy its base URL. In classic LM Studio, use **Developer → Start server**. Query its models:

```powershell
(Invoke-RestMethod -Uri "http://localhost:1234/v1/models").data | Select-Object id
```

Set these keys in `.env`, replacing existing entries or adding missing ones. Use the displayed URL and exact returned model ID:

```dotenv
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://localhost:1234/v1
LLM_MODEL=<exact ID from the command above>
LLM_STRUCTURED_MODE=json_schema
LLM_TIMEOUT_SECONDS=600
```

Load the model with at least the default `8192` context tokens in its server settings. The workbench's context budget does not change the server's loaded context. See the [detailed Bionic/Gemma configuration](MODELS-AND-DEBUGGING.md#use-a-model-served-by-lm-studio) for authentication, other model sizes and structured-output requirements. [Bionic local API instructions](https://github.com/meta-models/meta-oss-cookbook/blob/main/inference-server/lm-studio.md)

The 600-second timeout is a starting suggestion for slow CPU inference, not a guarantee. A tested CPU-only Llama repair request exceeded it. Longer waits may still fail; use a model that fits your hardware and review the actual run's logs.

**Or use Ollama:** Keep the default `.env` (`LLM_PROVIDER=ollama`, `OLLAMA_MODEL=llama3.1:8b`), start Ollama and download the model:

```powershell
ollama pull llama3.1:8b
ollama list
```

The saved download is reused on later runs. Another downloaded Ollama tag can be selected in `.env`. [Ollama Windows documentation](https://docs.ollama.com/windows)

## 4. Check and start the application

Keep the chosen model server and container engine running:

```powershell
.\.venv\Scripts\python.exe scripts\doctor.py
.\.venv\Scripts\python.exe scripts\check_model.py --generate
```

`doctor.py` succeeds only when the selected model service, model, Docker and sandbox image are available. The generation check makes a real structured request; it does not establish full workflow success. Resolve failures using [troubleshooting](../README.md#troubleshooting), then start the application:

```powershell
.\scripts\start.ps1
```

Open [http://localhost:8501](http://localhost:8501) and leave the PowerShell window open. Upload `examples/data/customer_sales.csv` and try the sales problem in [examples/scenarios.md](../examples/scenarios.md). Review the proposals, then choose **Approve & implement**.

On later days, start your model server and container engine, open PowerShell in the checkout and run `.\scripts\start.ps1`. Packages, model weights and the sandbox image are reused. Saved workspaces remain in the local `workspaces` folder. The default sandbox allows four CPUs and 8 GB RAM in addition to the model's memory; adjust `.env` if needed.

For edits, open this same checkout in Bionic or an editor. Application code remains editable on your laptop, and generated solutions can be reviewed and rerun in a new sandbox workspace. Follow the [code and debugging guide](MODELS-AND-DEBUGGING.md#edit-and-debug-the-code).
