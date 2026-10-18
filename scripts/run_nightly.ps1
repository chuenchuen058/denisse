# Wrapper for Task Scheduler: runs the nightly vault job with the repo's venv
# and keeps the last run's output where it can be read afterwards.
# Mirrors eval\run_scheduled.ps1 -- same pattern, same repo.
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$logs = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$out = Join-Path $logs "nightly_last_run.log"

Set-Location $repo
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $out -Encoding utf8
& $py (Join-Path $repo "scripts\nightly.py") *>&1 | Out-File -FilePath $out -Encoding utf8 -Append
exit $LASTEXITCODE
