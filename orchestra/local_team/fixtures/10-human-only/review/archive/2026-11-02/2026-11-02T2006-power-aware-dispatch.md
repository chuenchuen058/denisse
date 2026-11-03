# Orchestra: the runner decides about battery, not Windows killing it mid-task
Queue: auto
Priority: normal
Weight: light
Kind: work
Lane: claude
Written: 2026-11-02T20:06

Observed 2 Nov 2026 ~19:53 (`docs/orchestra_baseline_2026-11-02.md`): the "Denisse Orchestra" scheduled task has `DisallowStartIfOnBatteries` **and** `StopIfGoingOnBatteries`. Unplugging the laptop killed the runner ticks while their `claude -p` lanes kept running orphaned, and no new tick starts until AC returns. `orchestra/install_task.ps1` line ~13 sets `-DontStopIfGoingOnBatteries:$false -AllowStartIfOnBatteries:$false`.

Master prompt section 8: "Pause heavy background work on battery." The right owner of that decision is the runner, which can stop *dispatching* cleanly while letting running lanes finish and be collected.

1. `orchestra/power.py`: `power_status()` → `{"ac": bool|None, "battery_percent": int|None}` via `ctypes` `GetSystemPowerStatus` (no new dependency; None fields when unknown, e.g. a desktop with no battery → treat as AC).
2. In `runner.py` (re-read master first; several tasks edit it), before dispatching anything new: if on battery and config `dispatch_on_battery` is false (default false), don't start a lane; log `ON BATTERY: no new dispatch (n%)` at most once per 30 min, not every tick. If config `battery_floor_percent` (default 30) is reached while on battery, still let running lanes finish. Finishing / gate / merge / push of an already-finished lane is allowed on battery (it's short and saves work).
3. `orchestra/state_now.py` and `orchestra/status.py` show `power: AC` or `power: battery 87% (dispatch paused)`.
4. `orchestra/install_task.ps1`: change to `-DontStopIfGoingOnBatteries -AllowStartIfOnBatteries` so Windows no longer kills a tick mid-task, with a comment pointing to `dispatch_on_battery`. **Do not run or re-register the task** — the owner (or the coordinator) applies it after merge. Document the one command to apply it in `orchestra/README.md`.
5. Tests (`orchestra/test_power.py`): power_status parsing from a faked `SYSTEM_POWER_STATUS`; runner dispatch refused on battery and allowed on AC / unknown; the log throttle; a finished lane still integrates on battery (use the existing fake-lane harness in `orchestra/test_runner.py` if practical).

## Gate
```
.venv\Scripts\python.exe -m unittest orchestra.test_power -v
.venv\Scripts\python.exe -m unittest orchestra.test_runner
```
