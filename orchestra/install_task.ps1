# Registers the Orchestra runner with Windows Task Scheduler: one tick every 2 minutes,
# only while you're logged on, never two at once. NOT run automatically - run it yourself
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
$repo   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\pythonw.exe"
$runner = Join-Path $repo "orchestra\runner.py"
$action  = New-ScheduledTaskAction -Execute $python -Argument "`"$runner`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -DontStopIfGoingOnBatteries:$false -AllowStartIfOnBatteries:$false `
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

if ($Producer) {
    # Not on battery (same as the runner); a run that finds Ollama down or the GPU busy logs a skip.
    $pAction  = New-ScheduledTaskAction -Execute $python -Argument "-m orchestra.local_team produce" -WorkingDirectory $repo
    $pTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
    $pSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -DontStopIfGoingOnBatteries:$false -AllowStartIfOnBatteries:$false `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
    Register-ScheduledTask -TaskName "Denisse Orchestra Producer" -Action $pAction -Trigger $pTrigger `
        -Settings $pSettings -Description "Local-team producer every 30 min: scout + planner on Ollama, admission.py writes .review/queue (orchestra/README.md)" -Force
}
