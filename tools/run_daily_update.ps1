param([string]$PythonPath = 'python')
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $repoRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:GIT_TERMINAL_PROMPT = '0'
$env:GODEBUG = 'http2client=0'
& $PythonPath -m tools.daily_schedule --dispatch
exit $LASTEXITCODE
