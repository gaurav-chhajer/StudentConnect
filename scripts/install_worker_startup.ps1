# Registers the hosted-data worker with local inference; does not start it or overwrite another task.
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
foreach ($requiredPath in @($pythonPath, (Join-Path $projectRoot '.env'), (Join-Path $projectRoot 'scripts\start_hosted_worker.py'))) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw 'The local Python environment, private .env, and worker modules must exist before installation.'
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'data') -PathType Container)) {
    throw 'The private data directory must exist before installation.'
}
$taskName = 'StudentConnect Five Minute Worker'
$existingTask = Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -eq $taskName }
if ($existingTask) {
    throw 'The worker task already exists. Inspect it in Task Scheduler; this installer will not overwrite it.'
}
$currentAccount = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument '-B -m scripts.start_hosted_worker' -WorkingDirectory $projectRoot -ErrorAction Stop
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentAccount -ErrorAction Stop
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ErrorAction Stop
$principal = New-ScheduledTaskPrincipal -UserId $currentAccount -LogonType Interactive -RunLevel Limited -ErrorAction Stop
if ($PSCmdlet.ShouldProcess($taskName, 'Start hosted-database fetching, local similarity scoring, and qualifying hosted email at Windows sign-in')) {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $taskSettings -Principal $principal -Description 'Hosted database and email; five-minute ATS fetching; local similarity model; alerts at 60/100. Logs in data/hosted_worker.log.' -ErrorAction Stop | Out-Null
    Write-Output 'Worker startup task installed for the current user. It starts at the next Windows sign-in.'
}
