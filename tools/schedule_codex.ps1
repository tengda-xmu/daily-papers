param([switch]$Enable)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$scriptPath = Join-Path $repoRoot 'tools\start_codex.ps1'
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $scriptPath + '" -NoBrowser -PythonPath "' + $pythonPath + '"'
$action = New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') -Argument $arguments -WorkingDirectory $repoRoot
$owner = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$triggers = @(New-ScheduledTaskTrigger -AtLogOn -User $owner)
foreach ($subscription in @(
    '<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name="Microsoft-Windows-Power-Troubleshooter"] and EventID=1]]</Select></Query></QueryList>',
    '<QueryList><Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational"><Select Path="Microsoft-Windows-NetworkProfile/Operational">*[System[EventID=10000]]</Select></Query></QueryList>'
)) {
    $eventTrigger = New-CimInstance -ClientOnly -Namespace 'Root/Microsoft/Windows/TaskScheduler' -ClassName 'MSFT_TaskEventTrigger' -Property @{Enabled=$true; Subscription=$subscription}
    $eventTrigger.PSObject.TypeNames.Add('Microsoft.Management.Infrastructure.CimInstance#MSFT_TaskTrigger')
    $triggers += $eventTrigger
}
$principal = New-ScheduledTaskPrincipal -UserId $owner -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$settings.Enabled = [bool]$Enable
$task = New-ScheduledTask -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Description 'Start the private paper companion on login, wake and network recovery. Reuse a running service and the verified Codex runtime; never open a browser automatically.'
Register-ScheduledTask -TaskName 'DailyPapers-Companion' -InputObject $task -Force | Select-Object TaskName,State
