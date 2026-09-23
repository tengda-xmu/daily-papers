param([switch]$Enable)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$syncScript = Join-Path $repoRoot 'tools\sync_wechat.ps1'
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $syncScript + '" -PythonPath "' + $pythonPath + '"'
$action = New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') -Argument $arguments -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At '06:35'
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew
$settings.Enabled = [bool]$Enable
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Refresh subscribed WeRSS accounts and publish public metadata before the 07:00 paper digest. Sessions remain local.'
Register-ScheduledTask -TaskName 'DailyPapers-WeChat' -InputObject $task -Force | Select-Object TaskName,State
