# Registers the Orchestra runner with Windows Task Scheduler: one tick every 2 minutes,
# only while you're logged on, never two at once. NOT run automatically - run it yourself
# when the dry runs look right:
#   powershell -ExecutionPolicy Bypass -File orchestra\install_task.ps1
# Remove with:
#   Unregister-ScheduledTask -TaskName "Denisse Orchestra" -Confirm:$false
# Optional: -Canary also registers the daily agent health check (python -m orchestra.exams
# canary, one 30-second exam per agent) at 07:10. Remove with:
#   Unregister-ScheduledTask -TaskName "Denisse Agent Canary" -Confirm:$false
param([switch]$Canary)
$repo   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\pythonw.exe"
$runner = Join-Path $repo "orchestra\runner.py"
$action  = New-ScheduledTaskAction -Execute $python -Argument "`"$runner`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2)
# Battery: Windows must not decide. It used to refuse ticks on battery and kill running ones on
# unplug, orphaning their claude -p lanes (2 Nov 2026). The runner now decides: config
# dispatch_on_battery / battery_floor_percent in orchestra/config.json (orchestra/power.py).
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
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
