param([string]$PythonPath = 'python')
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $repoRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:GIT_TERMINAL_PROMPT = '0'
$logPath = Join-Path $repoRoot '.local\wechat-discovery.log'
try {
    & (Join-Path $PSHOME 'powershell.exe') -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $repoRoot 'tools\start_wechat.ps1') | Out-Null
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        try { $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8001/api/health' -TimeoutSec 2; if ($health.status -eq 'ok') { break } } catch {}
        Start-Sleep -Seconds 2
    }
    $savedErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $PythonPath connectors/wechat_sync/discover.py 2>&1 | Out-File -LiteralPath $logPath -Append -Encoding UTF8
        $discoveryExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedErrorPreference }
    $githubTarget = [Uri]'https://github.com'
    $githubProxy = [System.Net.WebRequest]::DefaultWebProxy.GetProxy($githubTarget)
    if ($githubProxy -and $githubProxy.AbsoluteUri -ne $githubTarget.AbsoluteUri) {
        $env:HTTPS_PROXY = $githubProxy.AbsoluteUri
        $env:HTTP_PROXY = $githubProxy.AbsoluteUri
    }
    & $PythonPath -m tools.publish_wechat --status-only 2>&1 | Out-File -LiteralPath $logPath -Append -Encoding UTF8
    if ($LASTEXITCODE -ne 0) { throw 'Discovery snapshot publishing failed' }
    exit $discoveryExit
} catch {
    "[$(Get-Date -Format s)] Discovery stopped; inspect local WeRSS authorization and discovery state." | Add-Content -LiteralPath $logPath -Encoding UTF8
    exit 1
}
