$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
try {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8001/api/health' -TimeoutSec 3
    if ($health.status -eq 'ok') { Write-Output 'WeRSS is already running at http://127.0.0.1:8001'; exit 0 }
} catch {}
$pythonPath = Join-Path $repoRoot '.local\werss-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'The local WeRSS Python environment is not installed.' }
$script = Join-Path $repoRoot 'tools\run_werss.py'
$env:PYTHONIOENCODING = 'utf-8'
$process = Start-Process -FilePath $pythonPath -ArgumentList @('"' + $script + '"') -WorkingDirectory $repoRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $repoRoot '.local\werss-server.out.log') -RedirectStandardError (Join-Path $repoRoot '.local\werss-server.err.log') -PassThru
$process.Id | Set-Content -LiteralPath (Join-Path $repoRoot '.local\werss-server.pid')
Write-Output 'WeRSS started on loopback; management URL: http://127.0.0.1:8001'
