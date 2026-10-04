param([switch]$Remove)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -Force
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -Force
$secretDirectory = Join-Path $env:LOCALAPPDATA 'MaiYeBang'
$secretPath = Join-Path $secretDirectory 'deepseek.key.dpapi'
if ($Remove) {
    if (Test-Path -LiteralPath $secretPath) { Remove-Item -LiteralPath $secretPath }
    Write-Output 'Saved DeepSeek key removed.'
    exit
}
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object System.Windows.Forms.Form
$form.Text = 'MaiYeBang - DeepSeek API key'
$form.Size = New-Object System.Drawing.Size(570, 280)
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$label = New-Object System.Windows.Forms.Label
$label.Location = New-Object System.Drawing.Point(22, 20)
$label.Size = New-Object System.Drawing.Size(510, 65)
$label.Text = "Enter your DeepSeek API key below. It stays on this PC, encrypted for your Windows user. It will not appear in chat, source code, or logs. Only the local backend uses it."
$box = New-Object System.Windows.Forms.TextBox
$box.Location = New-Object System.Drawing.Point(22, 92)
$box.Size = New-Object System.Drawing.Size(510, 28)
$box.UseSystemPasswordChar = $true
$box.MaxLength = 500
$note = New-Object System.Windows.Forms.Label
$note.Location = New-Object System.Drawing.Point(22, 132)
$note.Size = New-Object System.Drawing.Size(510, 42)
$note.Text = 'Saving configures the local app. Online checks make bounded, billable DeepSeek calls. No merchant payment is enabled.'
$save = New-Object System.Windows.Forms.Button
$save.Location = New-Object System.Drawing.Point(320, 185)
$save.Size = New-Object System.Drawing.Size(100, 30)
$save.Text = 'Save key'
$cancel = New-Object System.Windows.Forms.Button
$cancel.Location = New-Object System.Drawing.Point(432, 185)
$cancel.Size = New-Object System.Drawing.Size(100, 30)
$cancel.Text = 'Later'
$cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$save.Add_Click({
    $keyValue = $box.Text.Trim()
    if ($keyValue.Length -lt 20 -or $keyValue -match '\s') {
        [void][System.Windows.Forms.MessageBox]::Show('Please enter a valid API key without whitespace.')
        return
    }
    $secureValue = $null
    try {
        New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null
        $secureValue = ConvertTo-SecureString $keyValue -AsPlainText -Force
        $encrypted = ConvertFrom-SecureString $secureValue
        [System.IO.File]::WriteAllText($secretPath, $encrypted)
        $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $form.Close()
    } catch {
        [void][System.Windows.Forms.MessageBox]::Show('Unable to encrypt or save the key. Close this window and use the local setup page or the file import script. The key has not been printed.')
    } finally {
        $box.Clear()
        $keyValue = $null
        $encrypted = $null
        if ($null -ne $secureValue) { $secureValue.Dispose() }
    }
})
$form.Controls.AddRange(@($label, $box, $note, $save, $cancel))
$form.AcceptButton = $save
$form.CancelButton = $cancel
$result = $form.ShowDialog()
$form.Dispose()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output 'DeepSeek key saved with Windows user encryption.' }
else { Write-Output 'Key setup deferred; offline work remains available.' }
