param([switch]$Enable)
$ErrorActionPreference = 'Stop'
if ((Get-TimeZone).Id -ne 'China Standard Time') {
    throw 'This schedule uses Beijing time. Set the Windows time zone to China Standard Time before enabling it.'
}
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$scriptPath = Join-Path $repoRoot 'tools\run_daily_update.ps1'
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $scriptPath + '" -PythonPath "' + $pythonPath + '"'
$action = New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') -Argument $arguments -WorkingDirectory $repoRoot
$triggers = @('07:00', '07:17', '07:37', '08:17') | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$settings.Enabled = [bool]$Enable
$task = New-ScheduledTask -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Description 'Trigger the missing 07:00 Beijing paper update through authenticated GitHub CLI. Skip if updated today or a run is active; cloud scheduling remains enabled.'
Register-ScheduledTask -TaskName 'DailyPapers-MorningUpdate' -InputObject $task -Force | Select-Object TaskName,State
