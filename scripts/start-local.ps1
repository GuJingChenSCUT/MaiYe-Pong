param([int]$Port = 8765, [switch]$NoOpen, [switch]$Offline)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -Force
$workspacePath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $workspacePath 'checkpoint\engineering\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run scripts\setup-local.ps1 first.' }
$secretPath = Join-Path $env:LOCALAPPDATA 'MaiYeBang\deepseek.key.dpapi'
$env:PYTHONUTF8 = '1'
$env:DEEPSEEK_MODEL = 'deepseek-flash'
try {
if (-not $Offline -and (Test-Path -LiteralPath $secretPath)) {
    $secureKey = Get-Content -LiteralPath $secretPath -Raw | ConvertTo-SecureString
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try { $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer); $secureKey.Dispose() }
} else { Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue }
$runtimePath = Join-Path $workspacePath 'local_state'
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
$existing = $null
try { $existing = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/config" -TimeoutSec 2 } catch {}
if ($existing -and ($existing.capabilities.goods -ne 'synthetic_fixture' -or $existing.automatic_payment_dispatch -ne $false)) {
    throw 'This port is serving an unexpected application. Choose another port.'
}
if ($existing -and $Offline -and $existing.capabilities.model -ne 'blocked') {
    throw 'The existing server may use DeepSeek. Offline mode needs another port or an explicit server restart.'
}
if (-not $existing) {
    $runnerPath = Join-Path $PSScriptRoot 'run-local.py'
    $serviceProcess = Start-Process -FilePath $pythonPath -WindowStyle Hidden -ArgumentList @(('"' + $runnerPath + '"'), '--port', $Port) -PassThru -RedirectStandardOutput (Join-Path $runtimePath 'server.log') -RedirectStandardError (Join-Path $runtimePath 'server-error.log')
    [IO.File]::WriteAllText((Join-Path $runtimePath 'server.pid'), [string]$serviceProcess.Id)
    for ($i=0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 300
        if ($serviceProcess.HasExited) { throw 'Server exited. Inspect local_state\server-error.log (contains no API key).' }
        try { $existing = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/config" -TimeoutSec 1; break } catch {}
    }
    if (-not $existing) { throw 'Server did not become ready.' }
}
} finally { Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue }
if (-not $NoOpen) {
    Start-Process "http://127.0.0.1:$Port"
    & (Join-Path $PSScriptRoot 'show-access.ps1')
}
Write-Output "MaiYeBang: http://127.0.0.1:$Port"
