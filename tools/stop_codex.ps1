$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$connectionFile = Join-Path $projectDir '.local\codex-bridge\connection.json'
if (-not (Test-Path -LiteralPath $connectionFile)) { Write-Output 'Paper assistant is not running.'; exit 0 }
$connection = Get-Content -LiteralPath $connectionFile -Raw -Encoding UTF8 | ConvertFrom-Json
$baseUrl = 'http://127.0.0.1:43127'
$session = Invoke-RestMethod -Uri "$baseUrl/api/pair" -Method Post -ContentType 'application/json' -Body (@{code=$connection.pair_code} | ConvertTo-Json)
Invoke-RestMethod -Uri "$baseUrl/api/shutdown" -Method Post -Headers @{Authorization="Bearer $($session.token)"} | Out-Null
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 300
    try { Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 1 | Out-Null }
    catch { Write-Output 'Paper assistant stopped. Your VS Code Codex session remains open.'; exit 0 }
}
Write-Output 'Paper assistant is finishing shutdown. Your VS Code Codex session remains open.'
