# Registers the Orchestra runner with Windows Task Scheduler: one tick every 2 minutes,
# only while you're logged on, never two at once. NOT run automatically - run it yourself
# when the dry runs look right:
#   powershell -ExecutionPolicy Bypass -File orchestra\install_task.ps1
# Remove with:
#   Unregister-ScheduledTask -TaskName "Denisse Orchestra" -Confirm:$false
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
