# One eval run, for Windows Task Scheduler.
#
# Replaces the resident harness.ps1 loop, which didn't survive a week:
# it ran its own sleep loop in a visible window, so closing the window
# killed it (it vanished mid-run on 2026-09-12 and stayed dead), and it
# slept through target times when the laptop suspended. Task Scheduler
# handles missed runs, sleep and logon for us.
#
# Also fixes the other observed failure: after the 2026-09-09 reboot the
# harness started before Ollama was listening and burned its daily run on
# "Can't reach the local Ollama server". This waits instead.

param(
    [int]$WaitMinutes = 10   # how long to wait for Ollama before giving up
)

$ErrorActionPreference = 'Continue'
$Repo   = 'C:\Users\Xichu\GitHub\denisse'
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
$Log    = Join-Path $Repo 'eval\harness.log'

function Write-Line($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $Log -Value $line -Encoding utf8
    Write-Host $line
}

function Wait-ForOllama([int]$minutes) {
    $deadline = (Get-Date).AddMinutes($minutes)
    $announced = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri 'http://localhost:11434/api/tags' `
                 -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { return $true }
        } catch {
            if (-not $announced) {
                Write-Line 'waiting for Ollama to come up...'
                $announced = $true
            }
        }
        Start-Sleep -Seconds 20
    }
    return $false
}

if (-not (Test-Path $Python)) {
    Write-Line "FATAL: no python at $Python"
    exit 1
}

if (-not (Wait-ForOllama $WaitMinutes)) {
    Write-Line "SKIPPED: Ollama not reachable after $WaitMinutes min -- not counting this as a run"
    exit 3
}

Write-Line 'running eval suite...'
$out = & $Python (Join-Path $Repo 'eval\run_eval.py') --quiet 2>&1
$code = $LASTEXITCODE
foreach ($l in $out) { if ("$l".Trim()) { Write-Line "  $l" } }
Write-Line $(if ($code -eq 0) { 'all live cases passed' } else { "exit $code -- see the report" })
exit $code
