param([string]$PythonExe)
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $PythonExe) {
    $localVenv = Join-Path $repoRoot '.venv\Scripts\python.exe'
    $preparedVenv = [IO.Path]::GetFullPath((Join-Path $repoRoot '..\..\work\.venv\Scripts\python.exe'))
    if (Test-Path -LiteralPath $localVenv) { $PythonExe = $localVenv }
    elseif (Test-Path -LiteralPath $preparedVenv) { $PythonExe = $preparedVenv }
    else { throw 'Run scripts\setup.ps1 first, or supply -PythonExe with the Python environment containing requirements.txt.' }
}
Push-Location -LiteralPath $repoRoot
try {
    & $PythonExe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
} finally { Pop-Location }
