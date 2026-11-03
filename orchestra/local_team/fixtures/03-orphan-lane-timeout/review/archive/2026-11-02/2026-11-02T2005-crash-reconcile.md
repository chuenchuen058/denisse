# Orchestra: reconcile after a killed tick: dead-owner slot locks and orphaned lane reports
Queue: auto
Priority: urgent
Weight: heavy
Kind: work
Lane: claude
Written: 2026-11-02T20:05

Observed 2 Nov 2026 ~19:53 (evidence: `docs/orchestra_baseline_2026-11-02.md`, section "Power incident"). The laptop went onto battery; the scheduled task has `StopIfGoingOnBatteries`, so Windows killed both runner ticks (lock PIDs 13304 and 25428 gone) but **not** their `claude -p` children, which kept running orphaned. Consequences in today's code (`orchestra/runner.py`):
- `take_lock()` (~line 367) treats a slot lock as stale only by age (light/heavy timeout + `tick_watchdog_extra_s`, about 75 min). A dead owner blocks the slot that long.
- Nobody collects the orphan's work: its `.review/report.md` in the worktree, its commits on `review/<slug>`, its lane log (0 bytes, because the runner only writes output after the lane exits).
- When the slot frees, the task is re-dispatched as a fresh attempt, possibly while the orphan is still editing the same worktree.

Master prompt section 12 (`<private path>`): "verify process identity and liveness ... before stealing ownership. A reused PID must not make an unrelated process look like the old worker"; "On restart, reconcile before dispatching. Do not delete an uncertain worktree."

Build, in `orchestra/runner.py` (re-read master first: lane-rate-limits, campaign-controls and process-supervision tasks also edit runner.py; merge with what's there, don't revert it):
1. Slot lock records `pid`, the process **creation time** (via `ctypes` `GetProcessTimes`/`OpenProcess`; no new dependency), and the lane child's pid + creation time once started. Liveness = a process with that pid exists **and** its creation time matches. Dead or mismatched owner → the lock is stale immediately (log `RECONCILE dead owner <pid> for <slug>`). An unparseable lock falls back to today's age rule; never delete one because parsing failed.
2. At the start of every tick, before picking, reconcile each stale slot whose lock names a task:
   - lane child still alive → leave the slot occupied (log `RECONCILE orphan lane <pid> still running <slug>`); don't dispatch that task again; check again next tick.
   - lane child gone and the worktree has `.review/report.md` → finish the task through the normal `finish()` path (gate re-run, merge, push) exactly as if the lane had exited, with result taken from the report. Mark the history entry `recovered: true`.
   - lane child gone and no report → record NO_REPORT through the existing INFRA retry rule (counts as an attempt; no silent infinite retries). Keep the worktree.
3. Tests in `orchestra/test_runner.py` with the existing fake lane: dead-pid lock is reclaimed at once; pid reused by an unrelated process (same pid, different creation time — fake it by patching the liveness function) is treated as dead; live orphan blocks re-dispatch; orphan that left a report is collected and merged in a temp repo; orphan without report counts one attempt.
4. Two lines in `orchestra/README.md`: what reconcile does and the log lines it writes.

Don't edit `.review/state.json` or any live lock by hand, and don't touch the scheduled task.

## Gate
```
.venv\Scripts\python.exe -m unittest orchestra.test_runner -v
```
