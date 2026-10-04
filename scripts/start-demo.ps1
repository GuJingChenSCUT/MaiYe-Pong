# Open the dedicated local buyer demo. Existing model configuration is preserved.
[CmdletBinding()]
param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$workspacePath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $workspacePath 'checkpoint\engineering\.venv\Scripts\python.exe'
$statePath = Join-Path $workspacePath 'local_state'
$demoScriptPath = Join-Path $PSScriptRoot 'demo-account.py'
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
    & $shellPath -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'start-local.ps1') -Port 8765 -Offline -NoOpen
    if ($LASTEXITCODE -ne 0) { throw 'The offline local preview could not start. No replacement service will be launched.' }
    if ((Get-DemoServiceStatus) -ne 'ready') { throw 'The local preview did not become ready.' }
} else {
    Write-Output 'Reusing the healthy local preview; its model configuration is unchanged.'
}

# The helper owns private-file validation. No code is read into shell variables,
# command-line arguments, a URL, or a log; mismatched identity/state fails closed.
& $pythonPath $demoScriptPath --state-dir $statePath > $null
if ($LASTEXITCODE -ne 0) { throw 'The dedicated demo buyer is unavailable. No substitute identity will be created.' }
Start-Process -FilePath ($demoOrigin + '/?demo=start')
Write-Output 'Paste the code from the dedicated buyer window into the local access-code field.'
& $pythonPath $demoScriptPath --state-dir $statePath --show > $null
if ($LASTEXITCODE -ne 0) { throw 'The dedicated buyer window could not open. See docs\DEMO_READ_FIRST.txt for the local fallback.' }
