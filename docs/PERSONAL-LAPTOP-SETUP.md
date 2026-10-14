# Run on your personal Windows laptop

**Codex is not required.** This application runs independently with Python, Ollama and Docker Desktop. GitHub contains the application source and synthetic examples; model weights and the Docker sandbox image are downloaded or built separately on your laptop.

## 1. Install the prerequisites

Install these applications using their official Windows installers:

- [Git for Windows](https://git-scm.com/install/windows).
- [Python 3.11 for Windows](https://www.python.org/downloads/windows/), including the Python launcher. The setup script uses `py -3.11` by default.
- [Ollama for Windows](https://ollama.com/download/windows). Open Ollama after installation; it serves the local model API at `localhost:11434`. [Ollama Windows documentation](https://docs.ollama.com/windows)
- [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/), using its **WSL 2 backend** and **Linux containers**.

If WSL is not installed, open PowerShell **as administrator**, run the following command, and restart Windows when requested. [Microsoft WSL installation instructions](https://learn.microsoft.com/en-us/windows/wsl/install)

```powershell
wsl --install
```

Start Docker Desktop and wait for its engine to become ready. Docker Desktop must remain running while the workbench executes solutions. Its Windows documentation covers virtualization requirements and WSL updates if installation reports a problem.

## 2. Download the source

Open a **new, ordinary PowerShell window** in the folder where you want the project, then run:

```powershell
git clone https://github.com/barathj121/Agentic-AI-Framework.git
cd Agentic-AI-Framework
git --version
py -3.11 --version
docker info
docker info --format '{{.OSType}}'
```

The final command should print `linux`. If `docker info` fails, finish starting Docker Desktop before continuing. If GitHub requests authentication for the repository, sign in to the account with repository access.

## 3. Set up the application and model

From the same project directory:

```powershell
.\scripts\setup.ps1 -BuildSandbox
ollama pull llama3.1:8b
ollama list
.\.venv\Scripts\python.exe scripts\doctor.py
```

Setup creates `.venv`, installs Python packages, creates `.env` from the example if needed, generates the sample CSVs and builds the Docker sandbox image. The model download is separate and only needs to complete once. Keep Ollama running.

If PowerShell blocks the setup script, allow scripts for this PowerShell process only, then repeat the setup command:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\setup.ps1 -BuildSandbox
```

If you already have a supported Python interpreter but `py -3.11` cannot find it, use the script's explicit override: `.\scripts\setup.ps1 -PythonExe "C:\path\to\python.exe" -BuildSandbox`. The override must point to Python 3.11 or newer; the development host was tested with Python 3.12.

`doctor.py` exits successfully only when Ollama, the configured model, Docker and the sandbox image are all available. Consult the main [README troubleshooting section](../README.md#troubleshooting) for any reported failure.

## 4. Start and use it

```powershell
.\scripts\start.ps1
```

Open [http://localhost:8501](http://localhost:8501) in your browser. Leave this PowerShell window open. For a first run, upload `examples/data/customer_sales.csv` and use the sales problem in [examples/scenarios.md](../examples/scenarios.md). Review proposals and click **Approve & implement** before execution begins.

On later days, start Docker Desktop and Ollama, open PowerShell in `Agentic-AI-Framework`, and run `.\scripts\start.ps1`. You do not need to install packages or download the model again. Saved workspaces remain in the local `workspaces` folder.

The default sandbox allows four CPUs and 8 GB RAM, in addition to the model's memory usage. Adjust the documented settings in `.env` if your laptop needs smaller resource limits. CPU-only model inference can be slow.
