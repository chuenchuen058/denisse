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

| `python -m orchestra.local_team produce` | One producer pass on the local model: librarian, scout, planner, admission (Task Scheduler every 30 min with `install_task.ps1 -Producer`) |
| `python -m orchestra.local_team scout\|planner\|librarian\|status` | One role on its own (see "Local team and producer" below) |

Use the venv python (`.venv\Scripts\python.exe`) for all of them.

## Local team and producer

Until now new tasks came only from a Claude chat. The producer keeps the queue
supplied on its own, on the laptop's Ollama model (`local_team_model`, `llama3.1:8b`),
and a deterministic admission layer decides what actually enters the queue.

| Role | Reads | Writes (data only, `.review/local_team/`) | Can't |
|---|---|---|---|
| **scout** (`roles/scout.md`) | the delta since its last *successful* scan: signal lines of `.review/runner.log` (PARKED, FAIL, PARTIAL, broken gate, …), new `parked/*.why.txt`, NOT DONE / ODD bullets of new `archive/*/*.report.md`, failing suites in `.review/checks.json` (`scripts/run_checks.py --report .review/checks.json`). At most 25 items of 300 chars | `candidates/<stamp>.json`: problems, each tied to evidence ids it was given | read files, cite evidence it wasn't given (dropped), call the model when nothing is new |
| **planner** (`roles/planner.md`) | one candidate + its evidence, the gate registry, the test files that exist | `plans/<stamp>.json`: a task packet (title, objective, scope paths, acceptance, gate id + argument, weight); evidence is copied by code, not by the model | write a shell command, pick a gate outside `gates.json`, write the queue |
| **librarian** (`roles/librarian.md`) | one archived report (6000 chars) | `memory/<slug>.json`: ≤3 `FOR MEMORY:` lines with the report path and RESULT | write the vault or file the lines anywhere; credential- or address-looking lines are dropped |

Each role file has a version, its inputs, what it may and may not do, its budget
(one call) and its schema (`roles/<role>.schema.json`, also sent to Ollama as the
output `format`). Every answer is validated; a bad answer is logged and exits 1.

`produce` = librarian on ≤2 unread reports, then — only while the queue holds fewer
than `producer_target_ready` (6) tasks — one scout scan and planner + admission for
the ≤2 best non-low candidates. Nothing new → no model call; it waits for evidence.

**Admission** (`orchestra/admission.py`, no model) owns the queue write. It refuses:
a packet outside the schema or whose text could pass for a header, heading or code
fence; scope paths that aren't plain repo-relative paths, leave the repo, reach the
vault, or touch `.review/`, `.git/`, `.venv/`, `.claude/`, hooks, role prompts or the
policy files (`admission.py`, `gates.json`, `config.json`, `install_task.ps1`); a scope
spanning two areas; a gate not in `orchestra/gates.json` for that area, or whose
argument doesn't match its pattern or name an existing test; evidence whose quote is
no longer in its file (premise); duplicates of queued (incl. running), parked or
archived tasks or of earlier admissions/refusals (same evidence fingerprint, same
normalised goal, or a similar title with overlapping files — rewording doesn't get a
refused task back in); a queue at `producer_queue_cap` (20); more than
`producer_admit_per_hour` (4) in an hour. Every decision is a line in
`.review/local_team/admission.jsonl`; the full packet goes in `packets/<slug>.json`.

Admitted tasks are written as **`Queue: draft`**, with `Source:`, `Area:` and
`Evidence-Fp:` header lines. A Claude chat or check-in promotes one by reading it and
changing the line to `Queue: auto`. **To auto-admit an area** (skip the draft step),
add it to `auto_admit_areas` in `config.json`, e.g. `"auto_admit_areas": ["orchestra"]`
(area names and their gates are in `gates.json`). It starts empty.

**GPU.** One local inference at a time: each model call takes `.review/gpu.lease` in the
main checkout (worktrees share it) and *skips* if it is held. `eval/run_eval.py` takes the
same lease, waiting up to 5 minutes. A call is also skipped if Ollama is down, the model
isn't installed (it is never pulled), another model is loaded (ours would evict it),
or `nvidia-smi` shows the GPU ≥ `local_team_gpu_busy_pct` busy. `num_ctx` matches
Denisse's 8192 so the loaded model is reused. Skips go to
`.review/local_team/producer.log`; a skipped scan keeps its cursor.

Schedule (not registered automatically):
`powershell -ExecutionPolicy Bypass -File orchestra\install_task.ps1 -Producer`; not on
battery. Tests: `python -m unittest discover -s orchestra/local_team -p "test_*.py"` and
`python -m unittest orchestra.test_admission` (fake Ollama, temp repo).

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
