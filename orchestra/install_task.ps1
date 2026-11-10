# Registers the Orchestra runner with Windows Task Scheduler: one tick every 2 minutes,
# only while you're logged on, one tick per lane at once (parallel_lanes). NOT run automatically - run it yourself
# when the dry runs look right:
#   powershell -ExecutionPolicy Bypass -File orchestra\install_task.ps1
# Remove with:
#   Unregister-ScheduledTask -TaskName "Denisse Orchestra" -Confirm:$false
# Optional: -Canary also registers the daily agent health check (python -m orchestra.exams
# canary, one 30-second exam per agent) at 07:10. Remove with:
#   Unregister-ScheduledTask -TaskName "Denisse Agent Canary" -Confirm:$false
# Optional: -Producer also registers the local-team producer (python -m orchestra.local_team produce)
# every 30 minutes: scout + planner on the local Ollama model, tasks admitted by
# orchestra/admission.py (Queue: draft unless the area is in auto_admit_areas). Remove with:
#   Unregister-ScheduledTask -TaskName "Denisse Orchestra Producer" -Confirm:$false
param([switch]$Canary, [switch]$Producer)
# The scheduler ends a tick after this many minutes. It must outlast the runner's own limits:
# timeout_ceiling_s + tick_watchdog_extra_s (orchestra/config.json), or Windows kills the tick
# before the runner's timeout or watchdog can act and the run turns up as NO_REPORT (audit
# 10 Nov 2026, O3). orchestra/test_gate_timeout.py checks the two still fit.
$ExecutionTimeLimitMinutes = 120
$repo   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\pythonw.exe"
$runner = Join-Path $repo "orchestra\runner.py"
$action  = New-ScheduledTaskAction -Execute $python -Argument "`"$runner`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2)
# Battery: Windows must not decide. It used to refuse ticks on battery and kill running ones on
# unplug, orphaning their claude -p lanes (2 Nov 2026). The runner now decides: config
# dispatch_on_battery / battery_floor_percent in orchestra/config.json (orchestra/power.py).
# Parallel: parallel_lanes in orchestra/config.json runs one tick per lane at once, each with its own
# lock. IgnoreNew would drop Orchestra to one task at a time (the installed task was changed by hand).
$settings = New-ScheduledTaskSettingsSet -MultipleInstances Parallel -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $ExecutionTimeLimitMinutes)
Register-ScheduledTask -TaskName "Denisse Orchestra" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Runs one .review/queue task per tick (orchestra/runner.py)" -Force

if ($Canary) {
    $cAction  = New-ScheduledTaskAction -Execute $python -Argument "-m orchestra.exams canary" -WorkingDirectory $repo
    $cTrigger = New-ScheduledTaskTrigger -Daily -At "07:10"
    $cSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName "Denisse Agent Canary" -Action $cAction -Trigger $cTrigger `
        -Settings $cSettings -Description "One 30-second exam per agent; updates .review/exams/scores.json and the vault's Dev/Agent-Scores.md" -Force
}

if ($Producer) {
    # Not on battery: unlike the runner (whose battery policy lives in config.json), the producer is
    # pure local-GPU inference, so Windows may skip it unplugged. Ollama down / GPU busy logs a skip.
    $pAction  = New-ScheduledTaskAction -Execute $python -Argument "-m orchestra.local_team produce" -WorkingDirectory $repo
    $pTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
    $pSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -DontStopIfGoingOnBatteries:$false -AllowStartIfOnBatteries:$false `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
    Register-ScheduledTask -TaskName "Denisse Orchestra Producer" -Action $pAction -Trigger $pTrigger `
        -Settings $pSettings -Description "Local-team producer every 30 min: scout + planner on Ollama, admission.py writes .review/queue (orchestra/README.md)" -Force
}
