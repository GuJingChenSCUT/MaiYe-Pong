# Open the dedicated local buyer demo. Existing model configuration is preserved.
[CmdletBinding()]
param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$workspacePath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $workspacePath 'checkpoint\engineering\.venv\Scripts\python.exe'
$demoScriptPath = Join-Path $PSScriptRoot 'demo-short-accounts.py'
$demoOrigin = 'http://127.0.0.1:8765'

function Test-DemoPortListening {
    $probe = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $probe.BeginConnect('127.0.0.1', 8765, $null, $null)
        try {
            if (-not $pending.AsyncWaitHandle.WaitOne(5000)) {
                throw 'The local port check timed out. No service was started or changed.'
            }
            $probe.EndConnect($pending)
            return $true
        } finally { $pending.AsyncWaitHandle.Close() }
    } catch [System.Net.Sockets.SocketException] {
        # Only refusal means the port is free. Other network failures stay closed.
        if ($_.Exception.SocketErrorCode -eq [System.Net.Sockets.SocketError]::ConnectionRefused) {
            return $false
        }
        throw 'Cannot check the local demo port. No service was started or changed.'
    } finally { $probe.Close() }
}

function Get-DemoServiceStatus {
    if (-not (Test-DemoPortListening)) { return 'absent' }
    try {
        $config = Invoke-RestMethod -Uri ($demoOrigin + '/api/v1/config') -TimeoutSec 3 -MaximumRedirection 0
        $health = Invoke-RestMethod -Uri ($demoOrigin + '/api/v1/health') -TimeoutSec 3 -MaximumRedirection 0
    } catch {
        throw 'Port 8765 is occupied or unhealthy. No existing process or model configuration was changed.'
    }
    if ($config.capabilities.goods -ne 'synthetic_fixture' -or
        $config.capabilities.payment -ne 'local_simulator' -or
        $config.capabilities.deployment -ne 'local' -or
        $config.automatic_payment_dispatch -ne $false -or
        $health.automatic_payment_dispatch -ne $false) {
        throw 'Port 8765 is not the expected local preview. No account or service was changed.'
    }
    if ($health.status -ne 'ready' -or $health.storage.status -ne 'available' -or
        $health.worker.alive -ne $true -or
        $health.worker.status -notin @('starting', 'idle', 'running')) {
        throw 'The local preview is not ready. Preserve its state and inspect its health before retrying.'
    }
    return 'ready'
}

$serviceStatus = Get-DemoServiceStatus
if ($CheckOnly) {
    # Read-only diagnostics: never provisions an account, starts a server, or opens UI.
    Write-Output ('Local demo service: ' + $serviceStatus)
    exit 0
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Run scripts\setup-local.ps1 first, then open the demo launcher again.'
}
if ($serviceStatus -eq 'absent') {
    # Use a child shell so Offline does not change this shell's model environment.
    $shellPath = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $shellPath)) { $shellPath = (Get-Process -Id $PID).Path }
    & $shellPath -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'start-local.ps1') -Port 8765 -Offline -NoOpen -EnableDemoShortCodes
    if ($LASTEXITCODE -ne 0) { throw 'The offline local preview could not start. No replacement service will be launched.' }
    if ((Get-DemoServiceStatus) -ne 'ready') { throw 'The local preview did not become ready.' }
} else {
    Write-Output 'Reusing the healthy local preview; its model configuration is unchanged.'
}

$demoConfig = Invoke-RestMethod -Uri ($demoOrigin + '/api/v1/config') -TimeoutSec 3 -MaximumRedirection 0
if ($demoConfig.local_demo_short_codes_enabled -ne $true) {
    throw 'Short demo codes are disabled on the existing service. Restart it explicitly with scripts\start-local.ps1 -EnableDemoShortCodes. No service was restarted or changed.'
}
# Fixed test codes appear only in the requested UI, never in a URL or log.
# The server provisions and validates their separate buyer bindings atomically.
Start-Process -FilePath ($demoOrigin + '/?demo=start')
Write-Output 'Choose one local buyer from the demo window and enter its code on the page.'
& $pythonPath $demoScriptPath --show > $null
if ($LASTEXITCODE -ne 0) { throw 'The local demo buyer window could not open. See docs\DEMO_READ_FIRST.txt for the documented test codes.' }
