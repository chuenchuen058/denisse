# orchestra — the task runner

Day-to-day operation (schedule, results, safety layers, troubleshooting) is in
`docs/operator_guide.md`; the queue folders in `.review/README.md`; limits in
`config.json`, each with its reason. This file is the reference for every
command and mechanism.

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
| `python -m orchestra.exams canary --agent don` | The study tutor's own canary (`bank/study-don-1.json`, answered from the fixture vault the exam ships, never the real one). Add `don` to `exams_agents` in `config.json` to put him in the daily run |
| `python orchestra\campaign.py status\|pause\|resume\|stop\|emergency-stop\|disable-schedule` | Campaign deadline and Chuen's dispatch controls; see below |

| `python -m orchestra.local_team produce` | One producer pass on the local model: librarian, scout, planner, admission (Task Scheduler every 30 min with `install_task.ps1 -Producer`) |
| `python -m orchestra.soak --minutes 5 --seed 1` | Soak test: the whole runner against a temp repo with fake lanes, concurrent ticks, kills and power flips; invariants checked at the end (`docs/orchestra_soak.md`) |
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
(area names and their gates are in `gates.json`). It starts empty; see
`orchestra/local_team/BASELINE.md` for what the local model's proposals measured.

**Premise re-check before dispatch.** The sidecar `packets/<slug>.json` records a
fingerprint of each evidence file at admission (`evidence_files`: size + sha1 of those
bytes). The runner's `why_not_runnable` refuses a task with a sidecar if any of those
files is gone or its first `size` bytes changed: `premise moved, re-check`. Appending to
a log is not a move. After re-checking the task by hand, delete its sidecar to release it.
Tasks without a sidecar are not affected.

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

## The study tutor consulted before a lane (`consult.py`, `consult.json`)

Chuen, 5 Nov 2026: "treat Don like a professional opinion on task"; 9 Nov:
every agent goes through Don. Which tasks is the config rule
`consult_applies` (`config.json` and `config_defaults.json`): `all` (the
default) = every task, any lane; `named` = only a task whose header says
`Consult: don` or whose text names `study_brief`, `study_mode`, `tutor` or
`learner` as a file or folder (`consult.json` path_patterns); `off` = none.
A consulted task has `python -m tutor consult --json` run on its prompt in the
main checkout before the lane starts (a task about nothing study-related gets
the general question, `question_general`).

Quick, and never silently lost: a busy GPU (`.review/gpu.lease` held by
another job, or nvidia-smi at least `consult_gpu_busy_pct` busy) is waited
for, up to `consult_busy_wait_s`; the full consult then holds the lease and is
capped at `consult_timeout_s` (180 s; it was 420 s, and on 8 Nov two consults
timed out while the local model was busy). The tutor's local call is
schema-constrained JSON (tutor.json `model.structured_output`). Still busy, or
the full consult failed: `python -m tutor consult --notes-only --json`, no
model at all, the matching lines of his notes (`consult_notes_fallback`,
`consult_notes_timeout_s`). The `CONSULT <slug>:` line in `runner.log` says
`path=model`, `path=notes` (and why) or, when neither answered, `could not
answer (...)`; a checkout with no tutor is one `skipped` line.

The opinion (answer, confidence, sources, caveats, who answered, which path)
goes into the worktree as `.review/don.md`; the lane prompt gets a paragraph
naming it ("weigh it as a subject expert's view: advisory, never an override;
say under ODD where you go against it"); the reviewer prompt shows it
(`{{DON}}`, marked advisory, never a reason to block). The switch (`enabled`),
the header, the path patterns, the commands and every line of wording are in
`consult.json`. `ORCHESTRA_CONSULT_FILE` points tests at another copy. Tests:
`python -m unittest orchestra.test_consult` and the `consult` tests in
`orchestra.test_runner`.

## Seeing it run (`visible.py`, `toast.ps1`)

Chuen, 9 Nov 2026: "I want to see Claude Code running". When a lane starts,
the runner opens one console window titled "Orchestra live" running
`watch.py`, unless the one it opened is still running (`.review/live_window.json`
holds its pid and creation time); it is shown without taking the focus, and
never twice. A Windows toast says "Orchestra: <lane> started <title>" and
"Orchestra: <lane> finished: PASS|PARTIAL|... <title>". Switches and wording:
`live_window`, `live_window_title`, `live_window_argv`, `toasts`,
`toast_started`, `toast_finished`, `toast_argv`, `toast_timeout_s` in
`config.json`. A window or toast that fails is one log line, never a stopped
lane. If Task Scheduler runs the runner "whether the user is logged on or not",
it has no desktop and neither can be seen: run it as the logged-on user.

**A tab per lane run** (`lane_tab.py`, `visible.lane_tab`). Each claude, codex
or copilot run also gets its own Windows Terminal tab, "Claude: <task title>",
in one wt window named `orchestra`. The lane itself is unchanged: still
headless `claude -p --output-format stream-json`, so the report file, the
timeout and kill-on-close job, the cost read from the result event, the
worktree, the guard hook and the Don consult work exactly as before. The tab
is only a reader of `<slug>.lane.log` that shows it the way Claude Code does
(assistant text, each tool call with its file or command, Edit/Write diffs,
tool errors, the final result). When the lane exits the runner writes
`<slug>.lane.done`; the tab prints DONE with the lane's RESULT and closes after
`lane_tab_linger_s`. With no DONE (a killed runner) it gives up after the
timeout plus `lane_tab_extra_s`. No `wt.exe`: the same view in a plain console
(`lane_tab_fallback_console`). Interactive `claude` driven through the Stop hook
was the other option; it was not taken because the runner would lose the
exit code, the timeout kill, the result event's cost and the refused-tool
list, and a session can sit waiting for input. Off: `lane_tabs: false`.

## Autopilot: the queue refills itself (`atrium/autopilot.py`)

When no `Queue: auto` task is left, the laptop is on AC and a lane has
allowance, the runner asks the planner for the next tasks (one tick at a time,
`.review/autopilot.lock`). It reads the vault's `Atlas/Task Ideas.md` Inbox,
NOT DONE / ODD lines of recent reports, parked tasks (not those waiting on
Chuen) and hot.md's "Waiting on Chuen" (not Orchestra's own block). The local
model judges each one (actionable? repo-only? a bug fix? title, files). If it
can't answer, the fallback rule applies and the task is a draft. `Queue: auto` only
for repo-only work whose gate (`autopilot_gate`) can run; everything else is
`Queue: draft` and is listed in hot.md. Caps: `autopilot_max_per_hour`,
`_per_day`, `_per_refill`; never two tasks predicted to edit the same files;
never anything open, parked or done in the last 14 days. The lane is the one
with allowance (not LIMITED, spend cap not reached) and the fewest runs in
`autopilot_balance_hours`. Every decision is one `AUTOPILOT ...` line in
runner.log; the ledger is `.review/autopilot.json`. Off: `autopilot: false`.

**Bug fixes go to Claude.** `Kind: bugfix` (config `bugfix_kinds`), or the
planner's own reading (a PARTIAL/FAIL report's follow-up, a parked failure, the
model saying so), runs on `bugfix_lane` (claude) whatever the balance says,
unless Claude is limited (then the normal `lane_fallback`) or the task says
`Lane-Fixed: yes`. The START line says `(claude: bug fix; header lane ...)`.
Off: `bugfix_lane: ""`.

## Handing work to the queue (`new_task.py`)

Every agent that has real work for a repo hands it to the queue rather than
running a side job, so it shows in `runner.log`, STATE NOW and the hub
snapshot: `from orchestra.new_task import create` (`create(title, body=...,
gate=[...], lane=..., auto=True, source="<agent>")`, a `From:` header naming
the agent), or the agent hub's `queue_task` tool (default gate and source:
`hub_queue_default_gate`, `hub_queue_source`). The local team's planner
already goes through `admission.py`, which alone writes its tasks into the
queue. Not through the queue, on purpose: the hub's `ask_*` (a question
answered in the chat within a minute, not work to merge), the local team's
librarian and scout passes (read-only analysis whose output is admission's
input), and the daily exams (a measurement of the agents: as queue tasks they
would take lane slots and worktrees and be scored as work; they log
`EXAMS started` in `runner.log` and keep their scores in `.review/exams/`).

The agent hub does the same for chat work: `ask_don`, and `route_task` /
`split_tasks` put study-category work (the exams' `rules.json` title routes
and kinds) to the tutor first, with the opinion in front of the agent's prompt.

## Reconcile after a killed tick

Each slot lock (`.review/runner*.lock`) records the runner's and its lane child's pid and process creation time; before picking, every tick checks them: a dead (or pid-reused) owner frees the slot at once, a still-running orphaned lane keeps its slot and its task isn't re-dispatched, and a finished orphan is collected through the normal `finish()` path (its report → gate re-run, merge, push; no report → NO_REPORT, one counted attempt), with `recovered: true` in the history and the worktree kept.
Log lines: `RECONCILE dead owner <pid> for <slug>`, `RECONCILE orphan lane <pid> still running <slug>`, `RECONCILE collecting <slug>: lane gone, report found|missing`; an unreadable lock is never deleted for that alone and falls back to the old age rule.
With the job object below, an orphan only arises if the job couldn't be made (`lane_job: null` in the lock) or off Windows. Otherwise the lane dies with its runner, and reconcile finds a dead lock to collect.

## Process supervision (Windows Job Object)

Every lane and reviewer process starts through `runner.spawn_lane()`. It is created **suspended**, put in a fresh Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (ctypes, no dependency), and only then resumed (`NtResumeProcess`), so there is no window in which it can start a child outside the job. Only the runner holds the job handle (it is not inheritable). Closing it kills everything the lane started, including grandchildren whose parent already exited. `taskkill /T` walks parent pids and misses those.

The job closes:
- on **timeout** (`kill_tree` → `TerminateJobObject`),
- when the lane **exits normally**: any background children it left are killed too,
- when the **watchdog** fires (`close_all_jobs()` before `os._exit`),
- when the **runner dies** for any reason (crash, Task Scheduler ending it, its 2 h execution limit). Windows closes the handle and the job kills the tree.

The slot lock records `lane_pid`, `lane_created` and `lane_job` (the job's name, `orchestra-lane-<runner pid>-<n>`). If Windows refuses the job (create, set-limit or assign fails), the run goes ahead without one. The log says `job object …: … taskkill fallback`, `lane_job` is `null`, and a timeout falls back to `taskkill /T /F`. Lanes never ask for `CREATE_BREAKAWAY_FROM_JOB` (node/libuv deliberately doesn't), and breakaway isn't allowed. Tests: `python -m unittest orchestra.test_supervision -v` (Windows only). It covers a grandchild on timeout, an orphaned grandchild, a crashed runner, the watchdog, and the lock fields.

## When it runs: Task Scheduler, lock, sleep, logoff

Orchestra is **not a 24/7 service**. Checked on 2 Nov 2026 with `Get-ScheduledTask "Denisse Orchestra"`: principal `Xichu`, **LogonType Interactive**, RunLevel Limited, every 2 min, `StartWhenAvailable`, `DisallowStartIfOnBatteries`, `WakeToRun` off, `ExecutionTimeLimit` 2 h. No password is stored, so:

| Situation | What happens |
|---|---|
| Logged on, screen **locked** | Runs normally: the session still exists, and Interactive tasks run while it is locked. |
| **Logged off** | No ticks. Logoff ends the session's processes, so the runner dies, its job closes and the lane tree goes with it. The next tick after logon reconciles the dead lock (report → collected, none → NO_REPORT, one infra attempt). |
| **Sleep / hibernate** | No ticks (no wake-to-run). Processes are frozen, not killed, and resume where they were. Not measured: whether the lane's timeout and the watchdog count time spent asleep. A lane can be killed early after a long sleep. |
| **Resume / logon** | `StartWhenAvailable` runs one missed tick soon after. Reconcile runs before any dispatch. |
| **On battery** | No new ticks start. The installed task has `StopIfGoingOnBatteries` = False, so a running tick continues. If Task Scheduler does end a tick, the job takes its lane down with it. Before the job object, the lane survived that (the 2 Nov orphans). |
| **Offline** | Ticks run. Cloud lanes fail or time out and count under the infra retry rule. |

`install_task.ps1` doesn't match the installed task. The script says `-MultipleInstances IgnoreNew` and stop-on-battery; the live task is `Parallel` (needed for `parallel_lanes`) and doesn't stop on battery. Re-running the script as it is would bring back IgnoreNew.

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

## Master's own health (`master_health.py`)

Every gate includes `run_checks.py --fast`, so one test red on master used to make every session PARTIAL whatever it worked on (4 Nov 2026: 6 tasks; 5 Nov: 7; audit 10 Nov, H1). The runner now keeps `.review/master_health.json`: per master commit, whether the baseline (`master_health_baseline`, default `hub_queue_default_gate`) is green, red (with the modules that did not pass, read from run_checks' own `--- <module> (FAIL, exit 1) ---` lines; the tail if none match) or broken (timed out: holds nothing).

- **When it runs.** A tick that finds master at a commit with no record starts the baseline in a detached worktree `master-health-<commit>` (venv linked, removed after) as a hidden background process, like the exams: lock `.review/master_health.lock`, one at a time, never on battery or while paused, no lane slot, the tick never waits. Not checked yet counts as green. After a fast-forward, the commit the combined tree just tested is recorded green without a second run (its checks included `run_checks --fast: pass`). Log: `MASTER CHECK started ...` / `MASTER CHECK <commit>: red after 70s: <modules>`.
- **While master is red.** Every task whose `Kind:` is not in `bugfix_kinds` waits with "master is red: <modules>"; one `MASTER RED` line per 30 min; STATE NOW says so. If no `Queue: auto` bug fix is queued, one urgent bug-fix task naming the commit and modules is written through `new_task.create()` (From: `master-health`), once per red commit.
- **Park reasons.** A task parked from a branch whose base commit was red says so; if the runner's own gate failed only on those modules, it adds that the branch is not at fault.
- Run by hand: `.venv\Scripts\python.exe orchestra\master_health.py --commit <sha>`. `master_health: false` turns it all off.

## Conflict-aware dispatch and merge follow-ups

On 2 Nov 2026, with 3 Claude slots and Codex running at once, six PASSes could not be merged (merge conflicts, or a red combined tree because both sides had changed `eval/cases.py`, `orchestra/README.md`, `orchestra/config.json` ...) and each needed a person to queue a merge-only task. `orchestra/conflicts.py` now does three things, through small hooks in `runner.py`. Every value is a `conflict_*` key in `config.json`.

- **Predict.** A task's likely files are the paths named in its body (backticked or bare `orchestra/x.py`; a bare `runner.py` is resolved against `git ls-files`; paths under `conflict_ignore_prefixes`, URLs, globs and directories are ignored) plus a learned map in `.review/conflicts.json` (`conflict_map_file`): title/slug words -> the files that task's merged branch changed. The map is seeded from master's own `orchestra: merge review/<slug>` commits on first use (`conflict_bootstrap_merges`) and grows with every merge (`conflicts.py --bootstrap` rebuilds it; `--predict <task.md>` shows a prediction). A *running* task's files are what its worktree has changed so far (`git diff --name-only`, one per running task per tick) plus its prediction.
- **Dispatch.** `pick()` goes down the priority-sorted runnable list and takes the first candidate whose predicted files don't overlap a running task's files on a **hot** file (`conflict_hot_files`; shipped: `orchestra/runner.py`, `orchestra/config.json`, `eval/cases.py`, any `README.md`). A held candidate gets one log line, `held <slug>: overlaps <other> on orchestra/runner.py (hold 1/2)`, and a `conflict_holds` count in state. After `conflict_max_holds` holds it runs anyway; if every candidate overlaps, the first runs. Nothing running -> `runnable[0]`, no git calls, behaviour unchanged. A predictor error is logged and dispatch falls back to plain order. `--dry-run` shows the same choice and records nothing.
- **Recover.** When a PASS ends in `merge conflict; merge by hand`, or its combined tree fails while both sides changed the same files, the runner queues **one** follow-up `.review/queue/<slug>-merge.md` from `merge_followup_prompt.md` (Lane `conflict_followup_lane`, Priority `conflict_followup_priority`, the original Weight, header `Merge-Of: <slug>`): merge `review/<slug>` onto current master keeping both sides, re-run the original gate verbatim. The original keeps its old disposition (archived with its merge note, or parked) with `; follow-up <slug>-merge queued` added; state records `merge_followup`. When the follow-up merges, the original's merge note becomes `merged by <slug>-merge` and a parked original moves to the archive. A follow-up never gets a follow-up of its own, a second conflict of the same original never makes a second one, and a red tree with no shared files is left to a person as before. `conflict_followup: false` turns it off. This is the automatic form of `requeue.py <slug> --merge-only`, which stays for anything else.

Tests: `python -m unittest orchestra.test_conflicts -v` (temp repos, fake worker/merger lanes: prediction, hold and anti-starvation, one follow-up per original, the follow-up merging, values from config, no change when nothing overlaps).

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

## Campaigns and the dispatch controls (`campaign.py`)

A campaign is a time-boxed change to the limits — e.g. the Max-plan sprint,
Claude ×3 and a $400/day notional cap until Wed 4 Nov 18:00 London. The
deadline lives in code, not in a chat that has to remember to edit
`config.json`. `config.json` always holds the **normal** values.

**`.review/campaign.json`** (private, git-ignored), schema 1:

```json
{"schema": 1, "id": "max-plan-sprint-2026-11", "purpose": "...", "enabled": true,
 "started_at": "2026-11-02T20:00:00Z", "stop_at": "2026-11-04T18:00:00Z",
 "stop_at_source": "Chuen in chat, 2 Nov", "timezone": "Europe/London",
 "overrides": {"lane_slots": {"claude": 3, "codex": 1}, "daily_spend_cap_usd": 400},
 "restart_after_stop": false}
```

- Times are UTC with an explicit offset (`Z`); a naive time is refused.
  `timezone` is for display only. Keys starting `_` are comments.
- While `enabled` and `started_at <= now < stop_at`, `overrides` win over
  `config.json` (`lane_slots` merges per lane). Only `lane_slots` and
  `daily_spend_cap_usd` can be overridden. After `stop_at`, or when disabled
  or stopped, `config.json` applies again — the runner re-reads its limits
  every tick, so nothing needs editing. `ORCHESTRA_<KEY>` env vars still win
  for a single run.
- Validated on every load. Anything wrong (bad JSON, unknown or missing
  field, naive time, slots outside 1–8…) → the **whole file is ignored** and
  the reason goes to `.review/runner.log`. Never half applied, never a crash.
- `orchestra/campaign_seed.json` is the tracked copy of the sprint Chuen set
  in chat. The runner installs it as `.review/campaign.json` once — only if
  that file is missing and the seed's id was never installed — so deleting
  `.review/campaign.json` sticks. To change the campaign, edit
  `.review/campaign.json`.

**Controls**, which write `.review/control.json`:

| Command | Effect |
|---|---|
| `status` | Campaign, deadline and time left (London), the limits in force, each lane slot and its running task, whether dispatch is blocked |
| `pause` | No new task starts; running tasks finish. Reason in the queue: `paused by Chuen` |
| `resume` | Clears pause / stop / schedule-off. **Refused after the campaign's `stop_at`** (exit 2); `resume --after-deadline` is the explicit way to dispatch again on `config.json`'s normal values |
| `stop` | Pause, and end the campaign: its overrides stop at once. Reason: `campaign ended`. A later `resume` restarts dispatch but the campaign stays ended unless `restart_after_stop` is true |
| `emergency-stop` | `stop`, then kill the process trees named in the runner's own lock files (`.review/runner*.lock`: the lane PID and the tick waiting on it) and park those tasks as "cancelled by Chuen" with branch and worktree kept. Never touches a process it only finds running. A lane PID is only killed while its runner PID is alive (afterwards the number may be anyone's) |
| `disable-schedule` | Every tick (Task Scheduler, logon, by hand) starts nothing until `resume`. The scheduled task still fires and exits at once |

A pause, stop or schedule-off persists in `control.json`: no tick, watchdog,
reload or logon trigger clears it — only `resume`. The runner checks it in
`why_not_runnable` before every dispatch, and once more just before the lane
starts (a pause landing while the worktree was being made un-counts the
attempt). STATE NOW shows a `**Sprint:**` line (`ends Wed 18:00, 1 d 22 h
left, Claude ×3, Codex ×1, $400/day`, plus any block).

Workers can't change any of this: `hooks/guard.py` denies writes to
`campaign.json`, `control.json` and `campaign_seed.json`, Bash commands that
look like they write them, and `campaign.py` with any command but `status`.
That is a guard rail, not a sandbox (`python -c` with an obfuscated path
would get past it); the runner's gate re-run and PASS-only merge still apply.

Tests: `python -m unittest orchestra.test_campaign -v` (fake clock, fake lane,
mocked process kill).
