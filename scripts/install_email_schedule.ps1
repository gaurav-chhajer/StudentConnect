# Run manually to activate real email delivery for the current Windows user.
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$databasePath = Join-Path $projectRoot 'data\jobs.db'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Create the repository-local Python environment first.'
}
if (-not (Test-Path -LiteralPath $databasePath -PathType Leaf)) {
    throw 'Initialize the local database first.'
}
foreach ($credentialName in @('GMAIL_APP_PASSWORD', 'DIGEST_FROM_EMAIL')) {
    if ($WhatIfPreference) { continue }
    if (-not [Environment]::GetEnvironmentVariable($credentialName, 'User') -and
        -not [Environment]::GetEnvironmentVariable($credentialName, 'Machine')) {
        throw "Set $credentialName in the persistent Windows environment before scheduling."
    }
}
$taskName = 'StudentConnect Email Digests'
$existingTask = Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -eq $taskName }
if ($existingTask) {
    throw 'An email task already exists. Inspect it in Task Scheduler; this installer will not overwrite it.'
}
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument '-B -m scripts.run_local_email --scheduled --send' -WorkingDirectory $projectRoot -ErrorAction Stop
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15) -ErrorAction Stop
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ErrorAction Stop
$currentAccount = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentAccount -LogonType Interactive -RunLevel Limited -ErrorAction Stop
if ($PSCmdlet.ShouldProcess($taskName, 'Enable automatic real Gmail SMTP emails every 15 minutes when a digest is due')) {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $taskSettings -Principal $principal -Description 'Emails saved StudentConnect job scores at the profile cadence and local send time.' -ErrorAction Stop | Out-Null
    Write-Output 'Email task activated. It runs while this Windows user is logged in; disable it in Task Scheduler to stop it.'
}
