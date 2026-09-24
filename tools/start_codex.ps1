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
    & python -c "import fastapi, uvicorn, pypdf, pypdfium2, multipart, fontTools, reportlab, pymupdf, yaml, requests, PIL; assert pymupdf.VersionBind == '1.27.2.3'"
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
$connection = Get-Content -LiteralPath (Join-Path $runtimeDir 'connection.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $NoBrowser) { Start-Process "$baseUrl/#pair=$($connection.pair_code)" }
Write-Output "Paper assistant is running at $baseUrl/"
Write-Output 'The local page connects automatically. Pair the public website once and keep Remember this browser checked.'
