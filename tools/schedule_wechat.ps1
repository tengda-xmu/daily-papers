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
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Collect public WeChat article leads (at most 3 queries per day) before the 07:00 paper digest. Native restricted article endpoint is disabled.'
Register-ScheduledTask -TaskName 'DailyPapers-WeChat' -InputObject $task -Force | Select-Object TaskName,State
$discoverScript = Join-Path $repoRoot 'tools\discover_wechat.ps1'
$discoverArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $discoverScript + '" -PythonPath "' + $pythonPath + '"'
$discoverAction = New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') -Argument $discoverArguments -WorkingDirectory $repoRoot
$discoverTrigger = New-ScheduledTaskTrigger -Daily -At '18:00'
$discoverSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
$discoverSettings.Enabled = [bool]$Enable
$discoverTask = New-ScheduledTask -Action $discoverAction -Trigger $discoverTrigger -Principal $principal -Settings $discoverSettings -Description 'Discover research-related WeChat accounts; at most 2 searches and 3 additions per day. No immediate article collection.'
Register-ScheduledTask -TaskName 'DailyPapers-WeChat-Discovery' -InputObject $discoverTask -Force | Select-Object TaskName,State
