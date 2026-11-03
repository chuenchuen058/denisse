RESULT: PASS
GATE: .venv\Scripts\python.exe -m unittest orchestra.test_runner -v -> exit 0, 37 tests OK (also orchestra.test_tools: 8 OK); does not cover: a real Task-Scheduler kill on battery with a real `claude -p` child; parallel_lanes mode end to end (tests run single-lock because ORCHESTRA_REPO disables aftercare/parallel); two ticks reconciling the same lock at once; the Linux /proc creation-time branch (only Windows ctypes path exercised); the auto-push after a recovered merge (aftercare off in tests)
HUMAN: none
PREMISE: wrong in part: take_lock() already freed a lock whose pid was dead at once (commit 651cfff), so "stale only by age" was no longer true; but it used the pid alone (a reused pid looked alive), threw away the lock's task name, and nothing tracked or collected the orphaned lane child. The rest of the premise held.
DONE:
- runner.py: proc_created() via ctypes OpenProcess/GetProcessTimes (no new dependency); _alive(pid, created) = pid exists AND creation time matches; lock_status() -> live/orphan/dead/unknown
- Slot lock now records pid + created (take_lock, set_lock_task + lane), and after Popen the lane child's lane_pid, lane_created, lane_since, plus the pre-run main-checkout status ("before") so a recovered run's stray-write check still works
- take_lock: dead/mismatched owner -> stale at once, logs `RECONCILE dead owner <pid> for <slug>`; orphan lane alive -> busy; unparseable lock -> today's age rule, never deleted for failing to parse
- reconcile(state) at the start of every non-dry-run tick, before picking: orphan alive -> slot kept, task excluded from this tick's pick, logs `RECONCILE orphan lane <pid> still running <slug>`; lane gone -> claims the lock (under .review/reconcile.lock, rewritten with our live pid so no other tick dispatches it), copies the worktree's report, runs the normal finish() (gate re-run, merge, push / NO_REPORT under INFRA retry rule), marks history entries recovered: true, writes the vault note, frees the slot; worktree kept
- test_runner.py: 7 new tests (creation time identifies a process; dead-pid lock reclaimed at once; reused pid treated dead via patched proc_created; unparseable lock kept; live orphan blocks re-dispatch; orphan report collected and merged in a temp repo; orphan without report counts one attempt then parks after the retry)
- orchestra/README.md: "Reconcile after a killed tick" section, two lines (what it does, the log lines)
NOT DONE:
- No kill/timeout for an orphan lane that never exits: per the task it keeps its slot and is re-checked each tick, so a hung orphan blocks that slot until it exits or someone kills it
- The 0-byte lane log (claude buffering) is not addressed; collection reads whatever the orphan wrote
ODD:
- When an old-format lock has no "created" field, liveness falls back to the pid alone (can't verify identity); new locks always carry it
- When a lock has no recorded "before" status (old locks), the stray-write check uses the main checkout's status at reconcile time, so stray writes by that orphan can't be attributed
- A lock that names a fallback lane restores lane/lane-from on the reconstructed Task so history shows fallback_from as in a normal run
- An exception while collecting is logged and the lock is still freed (attempt was already counted at start), to avoid a crash loop on the same leftover
- Git warned runner.py is stored LF and will be CRLF-normalised on touch; content-only diff (183+/28-), no whole-file churn
COMMITS: f41fe2a
