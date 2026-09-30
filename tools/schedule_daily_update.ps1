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
$triggers = @('21:00', '21:17', '21:37', '22:17') | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$owner = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$triggers += New-ScheduledTaskTrigger -AtLogOn -User $owner
$subscriptions = @(
    '<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name="Microsoft-Windows-Power-Troubleshooter"] and EventID=1]]</Select></Query></QueryList>',
    '<QueryList><Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational"><Select Path="Microsoft-Windows-NetworkProfile/Operational">*[System[EventID=10000]]</Select></Query></QueryList>'
)
foreach ($subscription in $subscriptions) {
    $eventTrigger = New-CimInstance -ClientOnly -Namespace 'Root/Microsoft/Windows/TaskScheduler' -ClassName 'MSFT_TaskEventTrigger' -Property @{Enabled=$true; Subscription=$subscription}
    $eventTrigger.PSObject.TypeNames.Add('Microsoft.Management.Infrastructure.CimInstance#MSFT_TaskTrigger')
    $triggers += $eventTrigger
}
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$settings.Enabled = [bool]$Enable
$task = New-ScheduledTask -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Description 'Check the latest due 21:00 Beijing cycle on login, wake and network recovery. Resume missed updates across midnight; shared persistent queue prevents duplicate collection.'
Register-ScheduledTask -TaskName 'DailyPapers-MorningUpdate' -InputObject $task -Force | Select-Object TaskName,State
