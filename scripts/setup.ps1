param(
    [string]$PythonExe,
    [switch]$BuildSandbox,
    [switch]$SkipExamples
)
$ErrorActionPreference = 'Stop'
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Push-Location -LiteralPath $repoRoot
try {
    $venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        if ($PythonExe) {
            & $PythonExe -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"'
            if ($LASTEXITCODE -ne 0) { throw 'The -PythonExe interpreter must be Python 3.11 or newer.' }
            & $PythonExe -m venv .venv
        } elseif (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3.11 -m venv .venv
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"'
            if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 and retry.' }
            & python -m venv .venv
        } else { throw 'Python is missing. Install Python 3.11 from https://www.python.org/downloads/windows/ and reopen PowerShell.' }
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed. Install Python 3.11, or supply -PythonExe with a Python 3.11+ executable path.' }
    }
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed. Check your network/proxy settings.' }
    & $venvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
    if (-not $SkipExamples) {
        & $venvPython examples/generate_data.py
        if ($LASTEXITCODE -ne 0) { throw 'Example generation failed.' }
    }
    if ($BuildSandbox) {
        & docker info --format '{{.ServerVersion}}'
        if ($LASTEXITCODE -ne 0) { throw 'Docker engine is unavailable. Start Docker Desktop or Rancher Desktop with Moby/dockerd.' }
        & docker build -t agentic-workbench-sandbox:latest -f sandbox/Dockerfile .
        if ($LASTEXITCODE -ne 0) { throw 'Sandbox image build failed.' }
    }
    Write-Host 'Setup complete. Check dependencies with .\.venv\Scripts\python.exe scripts\doctor.py'
    Write-Host 'Start with .\.venv\Scripts\python.exe -m streamlit run app.py'
} finally {
    Pop-Location
}
