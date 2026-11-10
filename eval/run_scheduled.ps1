
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
#
# 2026-11-10: limits come from eval/eval.json (AGENTS.md Rule 0). A run the
# eval defers -- GPU still busy, lease still held, on battery; exit
# eval.json exit_codes.deferred -- books ONE retry, scheduled.retry_after_min
# later, as a one-shot task (AC only, same time limit as the nightly task),
# at most scheduled.max_retries_per_day a day. The run itself stays inside
# the task's limit: eval.json run_budget_s + ollama_wait_min is under
# scheduled.task_limit_s (the 'Denisse Eval' task's PT1H, which killed the
# 9 Nov run: result 267014).

param(
    [int]$WaitMinutes = -1,   # how long to wait for Ollama; -1 = eval.json scheduled.ollama_wait_min
    [switch]$DryRun           # log the retry it would book instead of registering it
)

$ErrorActionPreference = 'Continue'
$Repo   = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
$Log    = Join-Path $Repo 'eval\harness.log'
$Cfg    = Get-Content -Raw -Encoding utf8 (Join-Path $Repo 'eval\eval.json') | ConvertFrom-Json
$Sched  = $Cfg.scheduled
if ($WaitMinutes -lt 0) { $WaitMinutes = [int]$Sched.ollama_wait_min }

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

function Request-Retry {
    # Today's retry count lives in scheduled.retry_state (git-ignored).
    $statePath = Join-Path $Repo $Sched.retry_state
    $today = Get-Date -Format 'yyyy-MM-dd'
    $state = @{ date = $today; retries = 0 }
    if (Test-Path $statePath) {
        try {
            $old = Get-Content -Raw -Encoding utf8 $statePath | ConvertFrom-Json
            if ($old.date -eq $today) { $state.retries = [int]$old.retries }
        } catch { }
    }
    if ($state.retries -ge [int]$Sched.max_retries_per_day) {
        Write-Line "deferred, and $($state.retries) retries already booked today (max $($Sched.max_retries_per_day)) -- no retry; tomorrow's run will try again"
        return
    }
    $at = (Get-Date).AddMinutes([int]$Sched.retry_after_min)
    $name = $Sched.retry_task_name
    if ($DryRun) {
        Write-Line "DRY RUN: would book '$name' at $($at.ToString('HH:mm')) (retry $($state.retries + 1) of $($Sched.max_retries_per_day) today)"
        return
    }
    try {
        $action = New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') `
            -Argument "-ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File `"$PSCommandPath`"" `
            -WorkingDirectory $Repo
        $trigger = New-ScheduledTaskTrigger -Once -At $at
        # The battery rule: AC only, stop on unplug -- same as the nightly task.
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Seconds ([int]$Sched.task_limit_s)) `
            -MultipleInstances IgnoreNew
        $settings.DisallowStartIfOnBatteries = $true
        $settings.StopIfGoingOnBatteries = $true
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
            -Description 'One retry of a deferred Denisse eval run (eval/run_scheduled.ps1).' -Force | Out-Null
        $state.retries += 1
        $state | ConvertTo-Json | Set-Content -Encoding utf8 $statePath
        Write-Line "deferred -- retry booked as '$name' at $($at.ToString('HH:mm')) ($($state.retries) of $($Sched.max_retries_per_day) today)"
    } catch {
        Write-Line "deferred, and booking the retry failed: $($_.Exception.Message)"
    }
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
if ($code -eq [int]$Cfg.exit_codes.deferred) {
    Request-Retry
} else {
    Write-Line $(if ($code -eq 0) { 'all live cases passed' } else { "exit $code -- see the report" })
}
exit $code
