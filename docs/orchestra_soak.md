# Orchestra soak test

`orchestra/soak/` runs the whole runner under stress and checks invariants at the end. Every
piece of Orchestra (parallel lanes and slots, merged state saves, the merge lock, combined-tree
integration and the reviewer, rate limits, power, Job Objects, crash reconcile, provenance, the
aftercare push) had unit tests; nothing tested them together with ticks dying mid-run. Built on
3 Nov 2026 to reproduce the 2 Nov incident below. Stdlib only; nothing real is touched.

```commands
.venv\Scripts\python.exe -m orchestra.soak --minutes 5 --seed 1
.venv\Scripts\python.exe -m orchestra.soak --minutes 2 --seed 1 --keep --report .review\soak.md
.venv\Scripts\python.exe -m unittest orchestra.test_crash_recovery -v
.venv\Scripts\python.exe -m unittest discover -s orchestra\soak -p "test_*.py" -v
```

Exit 0 means every invariant held; 1 means the report (printed, and written with `--report`)
lists what broke. `--keep` leaves the temp repo for a look. The second line is what the task's
gate ran. Flags: `--tasks 40`, `--procs 6` (concurrent tick processes), `--kill-every 20`
(seconds between killing a random running tick), `--flip-every 15` (fake power flips).

## What it builds and does

- A temp repo with a bare `origin`, a `hot.md` shared file, a `tools/soak_gate.py` gate script
  (conflict markers, a `broken.flag`, a `budget/` cap of 2 entries that two passing branches can
  break together), and the real `orchestra/review_prompt.md`.
- ~40 tasks across three fake lanes (`alpha` x2 slots, `beta` x1, `gamma` x2; `beta` falls back
  to `alpha` when limited). Each task's `Soak:` header holds a JSON plan, deterministic from the
  seed: sleep, edit and commit, report PASS / PARTIAL / FAIL / none, a HUMAN question, print a
  `rate_limit_event` once, edit `hot.md` to force conflicts, touch `orchestra/` so the reviewer
  runs (and answer `ok` or `block`), add a budget entry, break the gate, write into the main
  checkout, commit a private-looking path (`drafts/`), spawn a grandchild that sleeps, or hang
  past the timeout. Every file the fake lane writes with non-ASCII text is UTF-8 with bytes
  that the console code page cannot decode (see bug 1).
- `orchestra/runner.py` of this checkout is started as a separate process per tick, like Task
  Scheduler's Parallel instances, with `ORCHESTRA_REPO` at the temp repo and
  `ORCHESTRA_AFTERCARE=1` (parallel lanes, the guarded push to the bare origin; `vault_dir` is
  empty so no vault note). Up to `--procs` ticks run at once for `--minutes`; every
  `--kill-every` seconds one running tick is killed outright (its Job Object takes the lane with
  it); the fake power state flips to battery for a few seconds every `--flip-every`.
- When time is up or the queue is empty, the harness drains running ticks, pauses dispatch
  (`campaign.pause`) and runs two more ticks: the first must reconcile what the kills left, the
  second must find nothing to do.

## Invariants

| Name | Holds when |
|---|---|
| `no_concurrent_runs` | No lane found another live lane on the same task (an O_EXCL marker per task, owner liveness checked), and no two slot locks ever named the same task in any 0.25 s poll. |
| `none_lost` | Every task file is in exactly one of `queue/`, `parked/`, `archive/<day>/`. |
| `state_sums` | `state.json` parses and each day's `work_s`, `maint_s`, `known_cost_s`, `estimated_runs` and `cost_usd` equal the sums over the history entries (reviewer time and cost included). |
| `no_dead_locks` | After the final ticks, no `.review/*.lock` remains whose owner is dead or unknown. |
| `master_merges_gated` | Every commit on master's first-parent line after the base is `orchestra: merge review/<slug>`, is the `integration.commit` in state, and its checks are green (or marked `recovered:`, bug 7). |
| `origin_gated` | `origin/master` is an ancestor of master, has no private-looking path (`aftercare.PRIVATE_PATHS`), and every `pushed` in state is really there. |
| `no_orphans` | No grandchild a lane spawned is still running (any found is killed and reported). |
| `worktrees` | Only parked tasks, "merge by hand" archives and tasks awaiting a retry keep a worktree; no `--integration` tree is left. |
| `ticks_clean` | No tick exited non-zero or printed a traceback (killed ones excepted); no `TICK CRASHED` or `WATCHDOG` in `runner.log`. |

The run also prints counts: ticks, kills, lane starts, reviews, merged / pushed / merge by hand
/ archived / parked (with reasons) / queued, history results, merge commits on master and on
origin. Seed 1 at 2 minutes, after the fixes below: about 400 ticks, 5 kills, 50 lane starts,
28 merged (26 pushed), 2 merge by hand, 10 parked, 0 queued, every invariant OK.

## The 2 Nov 2026 incident, reproduced

`runner.log`, 2 Nov: `23:25:12 PASS 2026-11-02T2145-multistep-read-found-file`, `23:25:15 runner
gate ... pass`, `23:25:15 review ... 427 changed lines (over 400)`, then nothing more from that
tick; `23:26:10 START ... attempt=2`; it ended "retry limit reached" with the PASS never merged.

Seed 1 reproduced the exact sequence on its first full run (task `soak-1-36`, a risky diff):
`PASS`, `runner gate ... pass`, `review soak-1-36: touches orchestra/notes.txt`, then the tick's
stderr (which pythonw discards) held
`UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d` raised in subprocess's reader
thread, followed by `TypeError: object of type 'NoneType' has no len()` at
`run_reviewer`'s `len(diff)`. One second later another tick logged `START soak-1-36 ...
attempt=2`, which crashed the same way, and the task sat in the queue at "retry limit reached"
with its worktree and PASS. Mechanism:

1. `git()` ran `subprocess.run(..., text=True)` with no encoding, so git's UTF-8 output was
   decoded as the console code page (cp1252). A diff holding a curly quote (U+201D, byte 0x9d)
   killed the reader thread; `communicate()` returned `stdout=None`.
2. `run_reviewer` did `len(diff)` on None. Nothing caught it; `tick()` had no handler and
   pythonw has no stderr, so the tick died with no log line and no state change.
3. `finally: release_lock()` freed the slot. The task had `attempts=1`, no history entry, no
   `last_result`, so `why_not_runnable` found it runnable and the next tick dispatched attempt
   2 at once, whose `fresh_worktree` deleted attempt 1's branch and worktree.
4. Attempt 2 crashed identically; `attempts=2 > infra_retries` gave "retry limit reached", a
   state no tick acts on, so it stayed in the queue forever with its PASS unmerged.

The slot lock itself was held correctly until `finish()` returned; it was the crash that
released it. On the second hypothesis in the task (the lock rewritten without `task`): not
found; `update_lock` keeps every field and nothing else writes the slot lock mid-task. The
regression test `test_slot_lock_names_the_task_through_gate_review_and_integration` now pins
that: its gate command reads the lock in the worktree and again in the integration tree.

## Why this task's own first two attempts "PASSed" but never merged

The main checkout's `runner.log` (read on the fourth attempt, 3 Nov 16:54) shows the incident
above happening to this task itself, on the runner that was still on master:

1. `02:59:44 PASS 2026-11-02T2332-orchestra-soak-test` (attempt 1, 3452 s), `03:05:47 runner
   gate ... pass: 2 command(s) exited 0` (so attempt 1's branch did hold `orchestra/soak/` with
   its package files; its report says they were `git add -f`'d past the ignore rule below),
   `03:05:47 review ... touches orchestra/README.md, orchestra/runner.py, orchestra/soak/... ;
   1608 changed lines (over 400)`, then nothing more from that tick. Its diff carried the curly
   quote on purpose (the regression fixtures), and master's `git()` still decoded with cp1252:
   bug 1, exactly as on 2 Nov.
2. `03:06:10 START ... attempt=2`, 23 s later: the crash had freed the slot. `fresh_worktree`
   deleted attempt 1's branch (its commits `ec2dacc` and `a8b2947` are still dangling in the
   object store) and started the session again from master.
3. `03:15:47 PASS ... after 575s (attempt 2)`, then `runner gate ... fail: No module named
   orchestra.soak`, then `PARKED`. Attempt 2's session had written no report in its worktree.
   `run_lane` cleared only the worktree copy, not `.review/reports/<slug>.md`, which still held
   attempt 1's PASS (the tick that died never archived or renamed it), so `finish()` read it as
   attempt 2's result: the parked report is attempt 1's byte for byte, naming attempt 1's
   commits. The gate then ran in attempt 2's bare worktree, hence the missing module. Bug 10.

So "it PASSed twice" was one PASS (attempt 1, lost to the re-dispatch) and one stale report.
The third attempt (11:08, this chain's commits `bb04173`, `892e3c5`, `3603dd1`) ended
`LIMITED` at 12:29 before its report was collected and was deleted the same way; the fourth
cherry-picked those three commits onto master and added bug 10.

Also fixed, not a runner bug: `.gitignore` had `_*.py` for a session's scratch files, and that
pattern also matches `__init__.py` and `__main__.py`, so a plain `git add orchestra/soak` skips
both and a fresh checkout of the branch has no package to run. `.gitignore` now ends that rule
with `!__init__.py` and `!__main__.py` (other packages' `__init__.py` had been force-added).

## Bugs found and fixed

Each fix is the smallest change that closes the hole, with a regression test in
`orchestra/test_crash_recovery.py` (or `orchestra/soak/test_soak.py`). "Seed" is the run that
showed it; with `--keep` the temp repo and every tick's output are left for inspection.

| # | Symptom | Cause | Fix | Test | Seed |
|---|---|---|---|---|---|
| 1 | The incident: PASS, gate pass, `review ...`, then the tick vanishes; the task is dispatched again; "retry limit reached", PASS never merged. | `git()` decoded with the console code page; `run_reviewer` crashed on `len(None)`; no handler, no log, slot freed. | `git()` reads UTF-8 with `errors="replace"`. Everything after a PASS runs in `_after_pass()` under one `try`: a runner exception now logs `RUNNER CRASHED <slug>: <Error> during review|integration ...` with the traceback, records `RUNNER_CRASH`, and parks the task with the branch kept and a `requeue.py <slug> --merge-only` hint. `tick()` logs `TICK CRASHED: ...` with the traceback for any exception before exiting, and `_record_crash()` counts a crash during a dispatched task as an INFRA attempt (one retry, then parked) instead of an immediate re-dispatch. | `test_git_output_is_utf8_whatever_the_console_code_page`, `test_pass_with_a_curly_quote_in_a_risky_diff_is_reviewed_and_merged`, `test_runner_crash_after_pass_parks_the_task_keeps_the_branch_and_logs_the_traceback`, `test_tick_crash_while_a_task_is_dispatched_is_logged_counted_and_parks_at_the_retry_limit`, `test_slot_lock_names_the_task_through_gate_review_and_integration` | 1, 40 tasks, 2 min (`soak-1-36`) |
| 2 | `state_sums` off by one run: a `LIMITED` entry vanished; the task then ran on the lane just found limited; `attempts` wrong. | The dispatching tick worked from the state it loaded at tick start. The tick that had just freed the slot saved its entry in between; `save_state` merges per task record, so the dispatcher's `attempts += 1` on the stale record overwrote the whole record. | Under the dispatch lock, after scanning the other slots, `_tick` re-reads `state.json` (and resets the merge base) before picking. | `test_dispatch_rereads_state_so_a_record_saved_meanwhile_is_not_overwritten` | 1 (`soak-1-30`, 11:41 run) |
| 3 | Two ticks starting together could run the same task in two slots. | The "already running" scan of other slot locks happened before taking a slot; two ticks could both see nothing, take different slots and pick the same top task (the window is milliseconds). | Pick and claim happen under `.review/dispatch.lock`, and the scan is done there, right before `set_lock_task`. | `test_dispatch_waits_for_the_dispatch_lock_and_skips_a_task_claimed_meanwhile` | found reading the code while building the harness; `no_concurrent_runs` never tripped |
| 4 | `TICK CRASHED: PermissionError: [WinError 32]` in `reconcile()` deleting `runner.gamma.2.lock`. | Windows refuses to delete a file another process has open; another tick was reading the lock at that instant. | `unlink_retry()` waits the read out (20 x 50 ms) and logs instead of raising; used for every lock and the power file. | `test_unlink_retry_waits_out_a_file_another_process_is_reading` | 1 (tick 391, first full run) |
| 5 | `TICK CRASHED: PermissionError: [Errno 13]` reading `queue/soak-1-33.md`. | Another tick was parking/archiving that file (`os.replace`) at that instant; `load_queue` / `header_problem` read every queue file. | `load_queue()` skips a file it cannot read; `header_problem()` returns "file unreadable right now"; `move()` retries a refused rename for a second. | `test_queue_listing_skips_a_task_file_that_cannot_be_read`, `test_move_waits_out_a_file_another_process_is_reading` | 1 (tick 29, 11:40 run) |
| 6 | A tick killed inside `state.lock` / `merge.lock` / `reconcile.lock` would stall every other tick for the stale time (120 s / 900 s) and crash them with `TimeoutError` after the wait; `merge.lock` left behind for good. | `_FileLock` wrote an empty file: no owner to check, only age. | `_FileLock` records pid + creation time; a dead holder is taken over at once; `reconcile()` sweeps dead brief locks (`RECONCILE merge.lock: holder <pid> is gone; removed`). | `test_file_lock_takes_over_a_dead_owner_at_once`, `test_file_lock_waits_for_a_live_owner`, `test_reconcile_sweeps_a_brief_lock_left_by_a_dead_process` | 2, 8 tasks, 0.6 min (`merge.lock` left by a killed tick) |
| 7 | Master had a merge commit whose task said `merge: "no commits"`, no integration record, never pushed. | The tick was killed between `git merge --ff-only` and its state save; the recovered run found the branch already contained and called it "no commits". | `integrate()` looks for that merge on master's first-parent line (`merged_already`: same subject, second parent = the branch tip), records it as `recovered:` and runs the push. | `test_merge_already_on_master_is_recovered_recorded_and_archived` | 2, 8 tasks, 0.6 min (`soak-2-06`) |
| 8 | Every tick died at import with `JSONDecodeError` when an `ORCHESTRA_<KEY>` env var was empty. | `env[:1] in "{["` is true for `""`, so `json.loads("")`. | `env[:1] in ("{", "[")`. | `test_empty_env_override_does_not_crash_config_load` | any (the soak sets `ORCHESTRA_VAULT_DIR=""`) |
| 9 | `no_concurrent_runs`: `runner.alpha.lock` and `runner.alpha.2.lock` named the same task at the same instant (the task ran twice at once), even with the dispatch lock of bug 3. | Slot locks were rewritten with `write_text`, which truncates first. `update_lock` (lane pid, main-checkout status) runs outside the dispatch lock, so a dispatcher scanning the slots at that instant read an empty lock, saw no task, and picked it. | `read_lock()` re-reads a lock that is empty, partial or momentarily unopenable (5 tries, 20 ms apart) before calling it unreadable; writes stay in place (`write_lock`). A temp file plus `os.replace` was tried first and is worse on Windows: the target is absent for an instant and readers get `FileNotFoundError`, and the rename fails while a reader holds the file open. | `test_lock_rewrites_are_never_seen_half_written` | 1, 40 tasks, 2 min, third run (`soak-1-22`); the two earlier seed-1 runs did not hit the window |
| 10 | A session that wrote no report was logged `PASS` with the previous attempt's report (this task, 3 Nov 03:15), and its gate ran in a worktree that held none of that work. | `run_lane` deleted only the worktree's `.review/report.md` before a run. `.review/reports/<slug>.md` from an earlier attempt survives when that attempt's tick died after PASS (no archive, no rename), and `finish()` reads whatever is there. | `run_lane` clears `.review/reports/<slug>.md` before starting the lane, as `_collect` already did. A lane that writes nothing is `NO_REPORT`. | `test_a_report_left_by_an_earlier_attempt_is_not_read_as_this_attempts_result` | not a seed: the fake lanes always write a report. Read from the main `runner.log` and the parked report. |

One harness lesson from the same run: after 20 measured runs `timeout_for` is
`max(timeout_floor_s, 3 x p95)`, and the default floor is 300 s, so the "hang" task could run
five minutes past the end of a 2-minute soak. The harness now sets `ORCHESTRA_TIMEOUT_FLOOR_S`
to the light timeout. The invariant that caught it (`ticks_clean`, "still running after the
end") now names the task the tick was on.

Not fixed, noted: with several ticks appending to `runner.log` at once a line can be lost (one
`PASS` line was missing for `soak-1-26` while state had its entry). Windows `O_APPEND` is not
atomic across processes. Rare at one tick per two minutes; `state.json` is the record.

## What the soak does not cover

- The real lanes (`claude -p`, Codex, Gemini): the fake lane prints only the result line and a
  `rate_limit_event`. Tool refusals, the guard hook, `--model` are untested here.
- Job Objects beyond "the grandchild dies with the lane" (`test_supervision.py` covers the rest).
- Vault notes and hot.md updates (`vault_dir` is empty), Task Scheduler itself, sleep/hibernate,
  the 2 h execution limit, the real `scripts/run_checks.py --fast` in the integration tree.
- Time: the gate runs 2 minutes, which finishes about 30 of 40 tasks on this laptop. A 5-minute
  run finishes them all. Run several seeds before trusting a change to the runner's locking.

Log lines added by these fixes, for `docs/operator_guide.md`'s troubleshooting table: `TICK
CRASHED`, `RUNNER CRASHED <slug>`, `<name>.lock: holder <pid> is gone; taken over`, `RECONCILE
<name>.lock: holder <pid> is gone; removed`, `could not remove <name>: still open in another
process`, `integration <slug>: merge <sha> already on master; recovering`.
