param([switch]$AllowProviderCharge, [ValidateSet('normal', 'clarification', 'shipping_increase', 'injection')][string]$Case)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -Force
if (-not $AllowProviderCharge) { throw 'Online tests are billable. Add -AllowProviderCharge to run the four bounded scenarios.' }
$workspacePath = Split-Path -Parent $PSScriptRoot
$engineeringPath = Join-Path $workspacePath 'checkpoint\engineering'
$pythonPath = Join-Path $engineeringPath '.venv\Scripts\python.exe'
$secretPath = Join-Path $env:LOCALAPPDATA 'MaiYeBang\deepseek.key.dpapi'
if (-not (Test-Path -LiteralPath $secretPath)) { throw 'Use scripts\configure-deepseek.ps1 to save your API key first.' }
$outputDirectory = Join-Path $workspacePath 'verification\refresh_20261003'
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$outputPath = Join-Path $outputDirectory ('live_model_' + [DateTime]::UtcNow.ToString('yyyyMMdd_HHmmss') + '_' + [guid]::NewGuid().ToString('N').Substring(0,8) + '.json')
$env:PYTHONUTF8 = '1'
$env:DEEPSEEK_MODEL = 'deepseek-flash'
try {
    $secureKey = Get-Content -LiteralPath $secretPath -Raw | ConvertTo-SecureString
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try { $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer); $secureKey.Dispose() }
    Push-Location $engineeringPath
    try {
        $caseArguments = @()
        if ($Case) { $caseArguments = @('--case', $Case) }
        & $pythonPath -X utf8 tests/test_agent_live.py --live-out $outputPath --allow-provider-charge @caseArguments
        $testExitCode = $LASTEXITCODE
    }
    finally { Pop-Location }
} finally { Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue }
Write-Output "Online evidence: $outputPath"
exit $testExitCode
