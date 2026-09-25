param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
$runtimeDir = Join-Path $projectDir '.local\codex-bridge'
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
$ownerName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $runtimeDir /inheritance:r /grant:r "${ownerName}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect the private paper assistant directory.' }
$baseUrl = 'http://127.0.0.1:43127'
$running = $false
try {
    $health = Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 2
    $running = $health.service -eq 'daily-papers-codex'
} catch { }
if (-not $running) {
    & python -c "import fastapi, uvicorn, pypdf, pypdfium2, multipart, fontTools, reportlab, pymupdf, yaml, requests, PIL, bs4; assert pymupdf.VersionBind == '1.27.2.3'"
    if ($LASTEXITCODE -ne 0) {
        & python -m pip install -r (Join-Path $projectDir 'connectors\codex_bridge\requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Could not install paper assistant dependencies.' }
    }
    $pythonExe = (Get-Command python).Source
    Start-Process -FilePath $pythonExe -ArgumentList '-m', 'connectors.codex_bridge.server' -WorkingDirectory $projectDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDir 'stdout.log') -RedirectStandardError (Join-Path $runtimeDir 'stderr.log') | Out-Null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $health = Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 1
            if ($health.service -eq 'daily-papers-codex') { $running = $true; break }
        } catch { }
    }
    if (-not $running) { throw "Paper assistant did not start. See $runtimeDir\stderr.log" }
}
if (-not $NoBrowser) {
    $connection = Get-Content -LiteralPath (Join-Path $runtimeDir 'connection.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $localPageUrl = "$baseUrl/#pair=$($connection.pair_code)"
    $edgeExe = $null
    foreach ($installRoot in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, $env:LOCALAPPDATA)) {
        if (-not $installRoot) { continue }
        $candidate = Join-Path $installRoot 'Microsoft\Edge\Application\msedge.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $edgeExe = $candidate
            break
        }
    }
    if (-not $edgeExe) {
        foreach ($registryPath in @(
            'HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe',
            'HKLM:\Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe'
        )) {
            $candidate = (Get-ItemProperty -LiteralPath $registryPath -ErrorAction SilentlyContinue).'(default)'
            if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
                $edgeExe = $candidate
                break
            }
        }
    }
    if (-not $edgeExe) {
        $edgeCommand = Get-Command msedge.exe -CommandType Application -ErrorAction SilentlyContinue
        if ($edgeCommand) { $edgeExe = $edgeCommand.Source }
    }
    if ($edgeExe) {
        Start-Process -FilePath $edgeExe -ArgumentList $localPageUrl | Out-Null
        Write-Output 'Opened the local paper assistant in Microsoft Edge.'
    } else {
        Write-Warning 'Microsoft Edge is not installed. Opening the local paper assistant in your default browser.'
        Start-Process -FilePath $localPageUrl | Out-Null
    }
}
Write-Output "Paper assistant is running at $baseUrl/"
Write-Output 'The local page connects automatically. Pair the public website once and keep Remember this browser checked.'
