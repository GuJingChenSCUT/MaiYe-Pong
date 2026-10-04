param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$workspacePath = Split-Path -Parent $PSScriptRoot
$engineeringPath = Join-Path $workspacePath 'checkpoint\engineering'
$venvPath = Join-Path $engineeringPath '.venv'
if (-not (Test-Path -LiteralPath (Join-Path $venvPath 'Scripts\python.exe'))) {
    & $Python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 or newer is required.' }
}
& (Join-Path $venvPath 'Scripts\python.exe') -m pip install -r (Join-Path $engineeringPath 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Output 'Ready. Run scripts\start-local.ps1.'
