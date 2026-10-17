param([string]$PythonExe, [switch]$StartServices)
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $PythonExe) {
    $localVenv = Join-Path $repoRoot '.venv\Scripts\python.exe'
    $preparedVenv = [IO.Path]::GetFullPath((Join-Path $repoRoot '..\..\work\.venv\Scripts\python.exe'))
    $runtimeConfig = Join-Path $repoRoot 'local-runtime.json'
    if (Test-Path -LiteralPath $localVenv) { $PythonExe = $localVenv }
    elseif (Test-Path -LiteralPath $runtimeConfig) {
        $PythonExe = (Get-Content -LiteralPath $runtimeConfig -Raw | ConvertFrom-Json).python_executable
        if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
            throw 'local-runtime.json points to a missing Python environment. Run setup.ps1 or supply -PythonExe.'
        }
    }
    elseif (Test-Path -LiteralPath $preparedVenv) { $PythonExe = $preparedVenv }
    else { throw 'Run scripts\setup.ps1 first, or supply -PythonExe with the Python environment containing requirements.txt.' }
}
$env:PYTHONDONTWRITEBYTECODE = '1'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    foreach ($dockerBin in @("$env:ProgramFiles\Rancher Desktop\resources\resources\win32\bin", "$env:ProgramFiles\Docker\Docker\resources\bin")) {
        if (Test-Path -LiteralPath (Join-Path $dockerBin 'docker.exe')) { $env:Path = $dockerBin + ';' + $env:Path; break }
    }
}
$ollamaDirectory = Join-Path $env:LOCALAPPDATA 'Programs\Ollama'
if (Test-Path -LiteralPath (Join-Path $ollamaDirectory 'ollama.exe')) { $env:Path = $ollamaDirectory + ';' + $env:Path }
if ($StartServices) {
    if (-not (Get-Process -Name 'Rancher Desktop','Docker Desktop' -ErrorAction SilentlyContinue)) {
        $rancher = Join-Path $env:ProgramFiles 'Rancher Desktop\Rancher Desktop.exe'
        $dockerDesktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
        if (Test-Path -LiteralPath $rancher) { Start-Process -FilePath $rancher -WindowStyle Hidden }
        elseif (Test-Path -LiteralPath $dockerDesktop) { Start-Process -FilePath $dockerDesktop -WindowStyle Hidden }
        else { Write-Warning 'Start a Docker engine before running an approved solution.' }
    }
    if (-not (Get-Process -Name 'ollama','ollama app' -ErrorAction SilentlyContinue)) {
        $ollamaApp = Join-Path $ollamaDirectory 'ollama app.exe'
        if (Test-Path -LiteralPath $ollamaApp) { Start-Process -FilePath $ollamaApp -WindowStyle Hidden }
        else { Write-Warning 'Start your configured local model server before requesting agent responses.' }
    }
}
Push-Location -LiteralPath $repoRoot
try {
    & $PythonExe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
} finally { Pop-Location }
