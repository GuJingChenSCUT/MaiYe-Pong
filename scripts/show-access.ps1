$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -Force
Add-Type -AssemblyName System.Windows.Forms
$credentialPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'local_state\access_private.json'
if (-not (Test-Path -LiteralPath $credentialPath)) { throw 'Start the local app first.' }
$codes = Get-Content -LiteralPath $credentialPath -Raw | ConvertFrom-Json
$dialog = New-Object System.Windows.Forms.Form
$dialog.Text = 'MaiYeBang - local development access'
$dialog.Width = 610
$dialog.Height = 270
$dialog.StartPosition = 'CenterScreen'
$dialog.TopMost = $true
$position = 20
foreach ($role in @('buyer','merchant','operator')) {
    $label = New-Object System.Windows.Forms.Label
    $label.Text = $role
    $label.SetBounds(15,$position,90,28)
    $textBox = New-Object System.Windows.Forms.TextBox
    $textBox.Text = $codes.$role
    $textBox.ReadOnly = $true
    $textBox.SetBounds(110,$position,460,28)
    $dialog.Controls.AddRange(@($label,$textBox))
    $position += 48
}
$note = New-Object System.Windows.Forms.Label
$note.Text = 'Copy the buyer code into the local app. Keep these local access codes private.'
$note.SetBounds(15,173,560,40)
$dialog.Controls.Add($note)
[void]$dialog.ShowDialog()
$dialog.Dispose()
