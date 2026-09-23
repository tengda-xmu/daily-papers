param([string]$PythonPath = 'python')
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $repoRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:GIT_TERMINAL_PROMPT = '0'
$logDir = Join-Path $repoRoot '.local'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logPath = Join-Path $logDir 'researchgate-sync.log'
try {
    "[$(Get-Date -Format s)] ResearchGate synchronization started" | Add-Content -LiteralPath $logPath -Encoding UTF8
    & $PythonPath connectors/researchgate_sync/export.py --headless 2>&1 | Out-File -LiteralPath $logPath -Append -Encoding UTF8
    if ($LASTEXITCODE -ne 0) { throw 'Collection failed; previous export preserved. Complete ResearchGate login or check access.' }
    # Git's CLI needs the Windows proxy setting explicitly; never print it.
    $githubTarget = [Uri]'https://github.com'
    $githubProxy = [System.Net.WebRequest]::DefaultWebProxy.GetProxy($githubTarget)
    if ($githubProxy -and $githubProxy.AbsoluteUri -ne $githubTarget.AbsoluteUri) {
        $env:HTTPS_PROXY = $githubProxy.AbsoluteUri
        $env:HTTP_PROXY = $githubProxy.AbsoluteUri
    }
    & $PythonPath -m tools.publish_researchgate 2>&1 | Out-File -LiteralPath $logPath -Append -Encoding UTF8
    if ($LASTEXITCODE -ne 0) { throw 'Metadata publishing failed; check GitHub authentication.' }
    "[$(Get-Date -Format s)] Synchronization completed" | Add-Content -LiteralPath $logPath -Encoding UTF8
    exit 0
} catch {
    "[$(Get-Date -Format s)] Synchronization stopped. See the preceding connector status; no retry was attempted." | Add-Content -LiteralPath $logPath -Encoding UTF8
    exit 1
}
