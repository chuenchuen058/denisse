RESULT: PASS
GATE: `.venv\Scripts\python.exe -m unittest orchestra.test_power -v` -> exit 0 (13 tests OK); `.venv\Scripts\python.exe -m unittest orchestra.test_runner` -> exit 0 (45 tests OK). Also ran test_integration/test_tools/test_aftercare (25 OK), and test_runner+test_integration with ORCHESTRA_POWER=battery:50 set outside (59 OK). Does not cover: the real GetSystemPowerStatus on battery (only checked live on AC, which read {'ac': True, 'battery_percent': 96}; the battery path runs on faked structs and the env override); the Task Scheduler settings change (install_task.ps1 was not run, as the task said); a real unplug while a lane is running.
HUMAN: none
PREMISE: right (install_task.ps1 had `-DontStopIfGoingOnBatteries:$false -AllowStartIfOnBatteries:$false`; the runner had no power check)
DONE:
- orchestra/power.py: power_status() via ctypes GetSystemPowerStatus -> {"ac", "battery_percent"}, None when unknown (unknown counts as AC; no battery, flag 128, gives percent None). Also parse(), dispatch_block(), describe(), and an ORCHESTRA_POWER=ac|battery|battery:N|unknown override.
- runner.py tick(): reconcile runs first (so a finished lane is still collected, gated, merged and pushed on battery). Then battery_pause() runs; if it blocks, the tick saves state, refreshes STATE NOW and returns "on battery" without taking a slot. It logs `ON BATTERY: no new dispatch (n%): <why>` at most once per 30 min (throttle kept in .review/power.json), and `POWER: AC; dispatching again` when power comes back. --dry-run prints the power line.
- config.json: dispatch_on_battery false, battery_floor_percent 30, each with its reason.
- state_now.py adds a `**Power:**` line. status.py adds `power: AC` / `power: battery 87% (dispatch paused)`, plus a `power` object in --json.
- install_task.ps1 now uses `-DontStopIfGoingOnBatteries -AllowStartIfOnBatteries`, with a comment pointing to dispatch_on_battery. It was not run. README "On battery" section gives the apply command: `powershell -ExecutionPolicy Bypass -File orchestra\install_task.ps1` (add -Canary if that task is registered).
- orchestra/test_power.py covers: parsing faked SYSTEM_POWER_STATUS (battery, charging, desktop, unknown), env override, floor, describe; dispatch refused on battery and allowed on AC and on unknown; dispatch_on_battery true stops at the floor; log throttle (1 line over 3 ticks, logs again after 31 min, resets on AC); a finished orphan lane still merges on battery while the next queued task is not started. It reuses test_runner's fake-lane harness.
NOT DONE:
- Re-registering the scheduled task (the task said not to; needs the owner or the coordinator after merge).
ODD:
- battery_floor_percent: running lanes are never killed for power. So the floor only matters when dispatch_on_battery is true: at or under the floor, no new dispatch. With the default (false) nothing dispatches on battery at all, so the floor has no effect.
- Added `ORCHESTRA_POWER=ac` to the setUp of test_runner.py and test_integration.py. Without it those suites would return "on battery" instead of PASS whenever the laptop is unplugged. No assertions changed.
- load_config's env override turns a string into a bool with bool(str), so ORCHESTRA_DISPATCH_ON_BATTERY=false would read as True. That code is older than this task and I left it alone; set the value in config.json, not by env.
- The diff touches orchestra/ and install_task.ps1, so the runner's reviewer lane will review it before merge.
COMMITS: 852a9e5 df65fe4
