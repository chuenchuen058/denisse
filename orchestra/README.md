# orchestra — the task runner

How the queue works is in `.review/README.md`; limits are in `config.json`,
each with its reason. This file covers the commands.

| Command | What it does |
|---|---|
| `python orchestra\runner.py [--dry-run]` | One tick: run the top runnable task (Task Scheduler runs this every 2 min) |
| `python orchestra\new_task.py "title"` | New task file in `.review/queue/` from the template |
| `python orchestra\status.py [--json]` | One screen: running tasks per lane slot, the queue with why each task isn't runnable, parked tasks with their reason, today's results and spend, the last 5 log lines. Reads only |
| `python orchestra\requeue.py <slug> [--note "why"]` | Put a parked task back in the queue |
| `python orchestra\state_now.py` | Regenerate `.review/STATE_NOW.md` (the runner does this after every tick) |
| `python -m orchestra.exams canary [--agent codex]` | Daily health check: one 30-second exam per agent; updates the scores and `<vault>/Dev/Agent-Scores.md` |
| `python -m orchestra.exams run [--agent X] [--category Y] [--include-claude]` | The full exam bank on every available agent (codex, copilot, gemini, `local:<model>`; claude only with `--include-claude`) |
| `python -m orchestra.exams best <category>` | The best working agent for `code-edit`, `debug`, `writing`, `study` or `extract` |

Use the venv python (`.venv\Scripts\python.exe`) for all of them.

## Agent exams and the score-based router

`orchestra/exams/` sends small auto-graded exams (`bank/*.json`, graded by
code, never by another model) to each agent and keeps the last 20 results
per agent x category in `.review/exams/scores.json`. An agent whose last 3
results failed, or that hasn't answered in 24 h, is "Not working" and is
never chosen. `claude` is chosen only if no other agent passes.

The scores steer two things, and both fall back to the old fixed rules when
there are no scores or nobody working has passed: the agent hub's
`route_task` / `split_tasks` (`agent_hub/server.mjs`), and the lane
`new_task.py` writes when no `--lane` is given (`exams/lane_for.py`; pass
`--work code-edit` etc. or let it guess from the title).

Run the canary daily so the scores stay fresh (24 h without an answer marks
an agent Not working):
`powershell -ExecutionPolicy Bypass -File orchestra\install_task.ps1 -Canary`
registers it at 07:10 alongside the runner. Tests:
`python -m unittest discover -s orchestra/exams -p "test_*.py"`.

## Reconcile after a killed tick

Each slot lock (`.review/runner*.lock`) records the runner's and its lane child's pid and process creation time; before picking, every tick checks them: a dead (or pid-reused) owner frees the slot at once, a still-running orphaned lane keeps its slot and its task isn't re-dispatched, and a finished orphan is collected through the normal `finish()` path (its report → gate re-run, merge, push; no report → NO_REPORT, one counted attempt), with `recovered: true` in the history and the worktree kept.
Log lines: `RECONCILE dead owner <pid> for <slug>`, `RECONCILE orphan lane <pid> still running <slug>`, `RECONCILE collecting <slug>: lane gone, report found|missing`; an unreadable lock is never deleted for that alone and falls back to the old age rule.

## On battery

The runner decides about battery, not Task Scheduler (`power.py`, `GetSystemPowerStatus` via ctypes; unknown, e.g. a desktop, counts as AC). With `dispatch_on_battery: false` (default) a tick on battery still reconciles, so a finished lane is gated, merged and pushed, but starts no new lane; it logs `ON BATTERY: no new dispatch (n%)` at most once per 30 min and `POWER: AC; dispatching again` when plugged back in. With `dispatch_on_battery: true` it dispatches until `battery_floor_percent` (default 30). Running lanes are never killed for power. `status.py` and STATE NOW show `power: AC` or `power: battery 87% (dispatch paused)`. `ORCHESTRA_POWER=ac|battery|battery:42` fakes the reading (tests).

`install_task.ps1` now registers the task with `-DontStopIfGoingOnBatteries -AllowStartIfOnBatteries`, so Windows no longer kills a tick on unplug. An already-registered task keeps its old settings until re-registered; to apply (re-registers "Denisse Orchestra" in place, `-Force`):
`powershell -ExecutionPolicy Bypass -File orchestra\install_task.ps1`
(add `-Canary` if the daily canary task is registered too, so it is kept current).

## Review and combined-tree integration

After the runner's own gate re-run passes, two more steps stand between a PASS and master:

1. **Reviewer** (`review_risky` in `config.json`): a diff touching `orchestra/`, `atrium/hook.py`, `.git/hooks`, any `install_*.ps1`, or over `review_max_lines` changed lines goes to a fresh read-only `claude -p` (Read/Glob/Grep, `git diff`/`git log` only, same guard hook) with the task, report and diff, using the versioned prompt `review_diff_prompt.md`. `VERDICT: block` — or no verdict, a timeout, a missing lane — parks the task with `<slug>.review.md` beside it. Its cost and time count like any lane. Override the reviewer's argv with a `"reviewer"` entry in `.review/lanes.json`.
2. **Combined tree**: the branch is merged (`--no-ff`) onto *current* master in a detached worktree `<slug>--integration`, the task's gate plus `scripts/run_checks.py --fast` (only if the script has that option) re-run there, and master is fast-forwarded to that exact tested commit under the merge lock. A red combined tree parks the task (master untouched, branch kept); if master moved while testing it redoes once, then parks as "master moved twice, retry". The dirty-checkout clash check and the aftercare push are unchanged.

State keeps per task `review` (verdict, reasons, why, seconds, spent) and `integration` (base, commit, checks, tries).

## Re-queuing a parked task

Don't edit `state.json` by hand. `requeue.py <slug>`:

- moves `.review/parked/<slug>.md` back to `.review/queue/` and appends
  `Re-queued <when>: <note>`, so the content (and hash) is new and the runner
  treats it as a fresh task;
- moves the parked report to `.review/reports/<slug>.attempt-parked.md`
  (`-2`, `-3`… if an earlier one is there) and deletes `<slug>.why.txt`;
- resets the task's `attempts` and `last_end` through the runner's own
  `load_state`/`save_state`, which merges with a lane runner saving at the
  same time;
- refuses if a runner lock (`.review/runner*.lock`) names the task as running,
  or if the queue already has a file of that name.

It prints whether the task is now runnable. It won't be if the reason it
parked still holds in its header, e.g. a `Written:` date past `stale_days`:
re-date the task in the queue.

Tests: `python -m unittest orchestra.test_tools orchestra.test_runner`.
