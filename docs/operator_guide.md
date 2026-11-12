# Operator's guide: Orchestra and Atrium

One page for running the thing day to day. The detail behind each part lives in
[`orchestra/README.md`](../orchestra/README.md) (every command and mechanism),
[`.review/README.md`](../.review/README.md) (the queue folders) and
[`docs/orchestra_security.md`](orchestra_security.md) (the safety layers). Limits and
their reasons are in [`orchestra/config.json`](../orchestra/config.json).

The runner's code holds no limits, patterns or lane commands of its own; they are all in
`config.json`, each with a `_<key>` note giving its reason:

| Key | What it controls |
|---|---|
| `infra_retries`, `backoff_min` | Retries after timeout / no report / lane missing, and the wait before one. |
| `timeout_s` (`light`, `heavy`), `timeout_min_runs`, `timeout_p95_factor`, `timeout_floor_s` | A lane's time limit per task weight, later learnt from past runs. |
| `gate_timeout_s`, `broken_gate_codes` | Gate time limit; exit codes that mean a broken gate (5, 127, 9009), not a failed one. |
| `lane_argv` | Each lane's command line. `{npm-shim:codex}` = the npm `.cmd` shim's full path; `{npm:<package>:<bin>}` = a global npm CLI run with node (lane left out if not installed). `.review/lanes.json` still overrides. `{prompt-file}` (inside an argument) = `<worktree>/.review/prompt.md`, holding the prompt, for a CLI that takes its prompt as an argument (the `copilot` lane: `-p` only points at that file). |
| `npm_dirs` | Where npm CLIs are looked for, in order, by the lanes, `orchestra/exams` and the agent hub (`%VAR%`, `~`, `*` allowed). Includes the Claude desktop app's packaged AppData, where npm installs made from the app really live. |
| `unpriced_lanes` | Lanes that report no cost, so the spend cap never blocks them. |
| `limit_patterns`, `limit_tail_lines`, `limit_default_min` | Usage-limit regexes per lane, how much of the lane log they read, and how long a lane waits when the message names no time. |
| `auth_patterns`, `auth_retry_min`, `auth_fix_hint` | The same for an expired sign-in. |
| `review_risky_patterns`, `review_max_lines`, `review_timeout_s`, `review_diff_max_chars`, `reviewer_argv` | Which diffs get the reviewer lane, its time limit, how much diff it sees, and its command. |
| `lane_auto_word`, `lane_strengths`, `lane_pacing`, `lane_pick_weights`, `lane_preference_bonus`, `bugfix_lane_bonus`, `lane_pick_explicit_scope`, `lane_usage_file` | How the runner picks a task's lane at dispatch (`orchestra/lane_pick.py`): `Lane: auto` (new_task.py's default) is scored over every lane by fit per work kind, pacing to each lane's reset, spend and free slots; an explicit `Lane:` is a strong preference; `Lane-Fixed: yes` never moves. The pick is written as `Lane-Picked:` / `Pick-Why:` in the task and `PICK <slug> -> <lane>: <why>` in runner.log. |
| `reviewer_lanes`, `reviewer_work`, `reviewer_override_lane` | Lanes that can review (each with its read-only command; `[]` = `reviewer_argv`), picked the same way and never the lane that wrote the change. `.review/lanes.json` `reviewer` still overrides. |

A bad value never crashes a tick. A key missing from `config.json`, or one of the wrong
shape (a string where a number belongs, a list item of the wrong type, an invalid regex,
an empty command, `timeout_s` without a weight), falls back to
[`orchestra/config_defaults.json`](../orchestra/config_defaults.json), which holds today's
values, and `.review/runner.log` says so: `config: <key> <why>; using the default <value>`.
An unreadable `config.json` falls back as a whole. Edit `config.json`, not the defaults.

Every command below is run from the repo root (`C:\Users\Xichu\GitHub\denisse`) with the
venv python. `orchestra/test_docs.py` checks that every script path in this guide's
command blocks exists.

## What runs where, and when

| Thing | Where it runs | When |
|---|---|---|
| **Runner** (`orchestra/runner.py`) | Task Scheduler task "Denisse Orchestra", from the main checkout | Every 2 minutes, **only while you are logged on** (no stored password). Screen locked is fine; logged off or asleep means no ticks. |
| **Task sessions** | Each in its own worktree, `..\denisse-wt\<slug>`, on branch `review/<slug>` | Started by a tick. Up to 3 Claude, 1 Codex and 1 Copilot at once (`lane_slots`). A task goes to Copilot only with `Lane: copilot`, until the daily exams have scored it. |
| **Power** | The runner checks the battery itself (`orchestra/power.py`) | On battery, ticks still run and finish off completed work (gate, merge, push), but **no new task starts** until you are back on mains. Running tasks are never killed for power. |
| **Producer** (local team, optional) | Task "Denisse Orchestra Producer", local Ollama model | Every 30 min if registered. Writes draft tasks only. |
| **Exams** | The runner, as a hidden background process (`orchestra/exams/daily.py`, lock `.review/exams.lock`) | First tick after 07:10 (`exams_daily_at`), once a day, not while any lane is limited. One canary exam each for `exams_agents` (codex, copilot, local; claude only with `exams_include_claude`; add `don` for the study tutor's own canary, answered from a fixture vault), 15 min cap in all. Writes `.review/exams/scores.json`, `Dev/Agent-Scores.md`, and a line in runner.log and the Orchestra day note. Takes no lane slot. The older opt-in Task "Denisse Agent Canary" (`install_task.ps1 -Canary`) is now redundant. |
| **Atrium capture** | A Claude Code hook (Stop, PreCompact, SessionEnd) | At the end of each Claude Code session; saves it into the vault. |

A tick: tidy up dead locks and finished lanes, then pick the top runnable task (urgent
first, your work before maintenance), make its worktree and start the session. When the
session writes its report, the runner re-runs the gate, reviews, tests the combined tree,
merges to master and pushes.

## Daily commands

What is happening now (read-only):

```commands
.venv\Scripts\python.exe orchestra\status.py
.venv\Scripts\python.exe orchestra\status.py --json
.venv\Scripts\python.exe orchestra\watch.py
.venv\Scripts\python.exe orchestra\watch.py --once
.venv\Scripts\python.exe orchestra\runner.py --dry-run
.venv\Scripts\python.exe -m orchestra.lane_pick explain <slug>
.venv\Scripts\python.exe -m orchestra.lane_pick migrate --dry-run
```

`status.py` is the one screen: running tasks per lane slot, the queue and why each task
is waiting, parked tasks and why, today's results and spend, the last log lines.
`watch.py` follows the running sessions live; the runner opens it itself, once, in a window
titled "Orchestra live" when a lane starts, and toasts each start and finish (config
`live_window`, `toasts`; orchestra/README.md, "Seeing it run"; since 12 Nov 2026 only for a lane
without its own tab, config `live_window_with_lane_tab`). Each lane run also gets its own
Windows Terminal tab, "Claude: <task title>", that shows the session the way Claude Code does
and closes itself after DONE (config `lane_tabs`). `runner.py --dry-run` shows what the next
tick would pick without running anything, with each runnable task's scores per lane and
the winner. `lane_pick explain <slug>` prints the same for one task; `lane_pick migrate
--dry-run` lists queued tasks whose `Lane:` could be `auto` (it never rewrites them).

When the queue has no `Queue: auto` task left, the autopilot planner writes the next ones from
the vault's Task Ideas Inbox, recent reports' NOT DONE / ODD, parked tasks and hot.md's waiting
items. It spreads them over the lanes with allowance left and sends bug fixes to Claude. Anything
not shown to be repo-only is a draft listed in hot.md. Since 12 Nov 2026 the report and parked
sources are switched off (config `autopilot_sources_off`; docs/orchestra_vs_thomas.md): it writes
tasks only from the Inbox and your own waiting items. Its decisions are the `AUTOPILOT` lines in
runner.log (config `autopilot`, `bugfix_lane`; orchestra/README.md, "Autopilot").

Writing a task:

```commands
.venv\Scripts\python.exe orchestra\new_task.py "what will be true when done"
.venv\Scripts\python.exe orchestra\new_task.py "fix the X bug" --priority urgent --weight heavy --lane claude
```

This makes `.review/queue/<stamp>-<slug>.md` as **`Queue: draft`**. Fill in the body (the
facts a session with no memory of today needs, real paths) and the `## Gate` command
block, then change the line to `Queue: auto`. `--auto` skips the draft step. One job per
file. A task with no gate block is never merged automatically.

Approving a draft (yours, or one the producer wrote): open it in `.review/queue/`, check the
evidence and gate still make sense, change `Queue: draft` to `Queue: auto`. That's all.

Putting a parked task back:

```commands
.venv\Scripts\python.exe orchestra\requeue.py <slug> --note "what changed and what to do"
```

Read `.review/parked/<slug>.why.txt` and the parked report first. A blind re-run of a
PARTIAL or FAIL has never come back PASS; the note is how you change the outcome. Never
edit `state.json` by hand. If the work is done on `review/<slug>` but never merged (conflict,
integration reject), `requeue.py <slug> --merge-only [--note "..."]` takes it from parked or the
newest archive and marks it urgent; the runner then merges that existing branch itself (gate,
reviewer if risky, combined-tree integration) and runs no lane. A missing branch or a merge
that can't run (conflict, dirty main checkout) parks it with the reason. Whenever the runner
deletes or resets a `review/<slug>` branch that has commits not on master, it first saves them
as `refs/keep/<slug>-<UTC time>` (config `keep_ref_prefix`) and logs `KEPT ...`; recover with
`git branch <name> refs/keep/<slug>-<time>`.

A PASS parked only because no reviewer could run (`The PASS stands` in its why file) needs
none of this: at the start of each tick the runner re-queues it merge-only by itself once a
reviewer lane for it is free (log `AUTO-REQUEUE <slug>: reviewer <lane> free`), oldest first,
`auto_requeue_review_max_per_tick` per tick, never one parked more than
`auto_requeue_review_max_age_days` ago. After `auto_requeue_review_max_attempts` tries (its
`Auto-Requeued:` header) it stays parked with "Auto-requeue stopped after N attempt(s)" in its
reason and is listed in hot.md "Waiting on Chuen": then run `requeue.py <slug> --merge-only`
yourself once you know why the reviewer keeps failing.

Pause, resume and stop (`orchestra/campaign.py`; the runner itself enforces these, and
only `resume` undoes them — no tick, watchdog or logon clears a pause):

```commands
.venv\Scripts\python.exe orchestra\campaign.py status
.venv\Scripts\python.exe orchestra\campaign.py pause
.venv\Scripts\python.exe orchestra\campaign.py resume
.venv\Scripts\python.exe orchestra\campaign.py stop
.venv\Scripts\python.exe orchestra\campaign.py emergency-stop
.venv\Scripts\python.exe orchestra\campaign.py disable-schedule
```

- **status** = the campaign (deadline in London time, time left, the slots and cap in
  force), each lane slot and what runs in it, and whether dispatch is blocked.
- **pause** = no new session starts (queue reason `paused by Chuen`). Running sessions
  finish and are still gated, merged and pushed.
- **resume** = dispatch again. Refused once the campaign's deadline has passed;
  `resume --after-deadline` says explicitly to go on with `config.json`'s normal limits.
- **stop** = pause, and end the campaign: its raised limits lapse at once (reason
  `campaign ended`).
- **emergency-stop** = stop, then kill the session trees named in the runner's own lock
  files (never a claude/python/ollama it merely finds running) and park those tasks as
  "cancelled by Chuen", branches kept.
- **disable-schedule** = every tick exits at once until `resume`. Same effect as
  `Disable-ScheduledTask -TaskName "Denisse Orchestra"`, but it survives a re-register
  and shows in `status`.
- **Hold one task** = change its `Queue: auto` back to `Queue: draft`.

A **campaign** (`.review/campaign.json`) is a time-boxed raise of the limits — the Max-plan
sprint is Claude ×3 and a $400/day cap until Wed 4 Nov 2026 18:00 London. At its `stop_at`
the runner goes back to `config.json`'s values by itself; nothing needs editing. STATE NOW
shows it as a `**Sprint:**` line. Details and the file's schema: `orchestra/README.md`.

Other occasional commands:

```commands
.venv\Scripts\python.exe orchestra\state_now.py
.venv\Scripts\python.exe -m orchestra.exams canary
.venv\Scripts\python.exe -m orchestra.exams best code-edit
.venv\Scripts\python.exe -m orchestra.local_team status
.venv\Scripts\python.exe -m orchestra.local_team produce
.venv\Scripts\python.exe -m atrium snapshot
.venv\Scripts\python.exe -m atrium context --lane codex
.venv\Scripts\python.exe -m atrium credits --days 7
.venv\Scripts\python.exe -m atrium.import_chats --source codex --vault "C:\Users\Xichu\OneDrive - King's College London\Denisse"
.venv\Scripts\python.exe scripts\run_checks.py --fast
.venv\Scripts\python.exe -m orchestra.soak --minutes 5 --seed 1
powershell -ExecutionPolicy Bypass -File orchestra\install_task.ps1
```

`atrium.import_chats` is a dry run until you add `--apply`. `install_task.ps1`
re-registers "Denisse Orchestra" in place (add `-Canary` / `-Producer` for those tasks).
`orchestra.soak` runs the whole runner against a throwaway repo with fake lanes, concurrent
ticks, kills and power flips, and checks invariants at the end; run it (a few seeds) after
touching the runner's locking or integration. Details and the bugs it found:
[`docs/orchestra_soak.md`](orchestra_soak.md).

## How results reach you

- **Vault, `Calendar/Orchestra/<date>.md`**: one section per finished task (result,
  merged or not, pushed or not, lane, minutes, cost, and the report's Done / Not done /
  Odd). Committed in the vault repo by the runner.
- **Vault, `STATE NOW.md`** (and `.review/STATE_NOW.md`): regenerated after every tick.
  Running, limited lanes, power, what's waiting on you, open queue, today's spend.
- **Atrium Hub page**: shows the JSON tables `python -m atrium snapshot` writes to
  `.review/hub/` (agents, tasks, waiting, log, spend).
- **Claude chat check-ins**: the chat reads STATE NOW and the parked folder, promotes
  drafts and re-queues parked tasks with a note. A HUMAN line in a report (money,
  credentials, sending as you, irreversible) always parks the task for you.
- **`.review/runner.log`**: one line per event. The troubleshooting table below is keyed
  on its wording.

## Safety layers

| Layer | What it does |
|---|---|
| **Guard hook** (`orchestra/hooks/guard.py`) | Claude lane only. Denies edits outside the worktree (after resolving links) and Bash commands that push, use `git -C`, `reset --hard`, `clean -f`, `gh`, recursive deletes, or name the main checkout. A PASS is not merged if the hook never ran. |
| **pre-push hook** (`orchestra/hooks/pre-push`) | Every lane. Installed into `.git/hooks` by each tick. Refuses any push from inside a worktree, so only the runner (from the main checkout) pushes. |
| **Gate re-run** | The session's PASS is a claim. The runner re-runs the gate block itself and merges only if every command exits 0. Exit 5 (no tests), 127 / 9009 (command not found) or a timeout is a **broken gate**, never a pass. |
| **Main-checkout check** | A PASS is not merged if the main checkout changed during the run. |
| **Reviewer** | A diff touching `orchestra/`, hooks or install scripts, or over 400 lines, goes to a fresh read-only Claude. `VERDICT: block`, no verdict or a timeout parks the task. |
| **Combined-tree check** | The branch is merged onto *current* master in a scratch worktree and the gate plus `run_checks.py --fast` run there. Master moves only to that tested commit. Red parks the task; master is untouched. |
| **Conflict-aware dispatch** (`orchestra/conflicts.py`) | Before a task starts, its likely files (paths in its body, a map learned from past merges) are compared with what running tasks are editing; an overlap on a hot file (`conflict_hot_files`: `runner.py`, `config.json`, `eval/cases.py`, READMEs) holds it while something else can run, at most `conflict_max_holds` times. A PASS that still can't merge for a conflict gets one `<slug>-merge` follow-up task instead of waiting for a person. |
| **Privacy check on push** (`orchestra/aftercare.py`) | Pushes master only, fast-forward only, and refuses if the commits touch private-looking paths (vault folders, `memory/`, `.review/`, keys) or add a line that looks like a key or token. The merge still stands locally. |

## What's not covered

- **Codex and Gemini have no tool guard.** Codex runs with full access. Only the gate,
  main-checkout, reviewer, combined-tree and push checks apply to them.
- **Code run by an allowed command** (a test, `python -c`, a script the session wrote)
  can write anywhere you can. The guard reads commands, it doesn't sandbox them.
- **Git-ignored files** in the main checkout (`.venv`, models) aren't seen by the
  main-checkout check.
- **The vault and anything outside the repo** aren't protected from commands.
- **Real cost.** The spend cap uses Claude's own notional figure, and runs killed before
  reporting are *estimated*. Check the real balance on the dashboards now and then.
- **Sleep.** Whether a lane's timeout counts time asleep hasn't been measured.
- **A gate only proves what it runs.** A narrow gate on a task that touched more code
  passes; the combined-tree `--fast` run is the backstop.

Full list: [`docs/orchestra_security.md`](orchestra_security.md).

## Troubleshooting

Rows are keyed on `.review/runner.log` wording and come from what happened on 2 Nov 2026.
The battery row is the one exception: that day's battery trouble shows up as dead locks,
because power-aware dispatch (which logs `ON BATTERY`) only landed that evening.

| You see | What it means | What to do |
|---|---|---|
| `lock runner.claude.lock held by dead pid 25428; removed` | A tick died (unplugged on battery before power-aware dispatch, logoff, crash) and left its slot lock. | Nothing: the next tick removes it and collects the run. Since Job Objects, the session dies with the runner, so there are no orphans to chase. |
| `ON BATTERY: no new dispatch (n%)` | On battery; nothing new starts. | Plug in. `POWER: AC; dispatching again` follows. |
| `MASTER RED <commit>: holding every task but bug fixes ...: <modules>` | Master's own baseline check (`orchestra/master_health.py`) fails at that commit, so any other task would fail its gate too. Only `Kind: bugfix` tasks run; one urgent bug-fix task is queued for it. | Let the bug-fix task run, or fix it by hand; the next tick re-checks the new master commit in the background (`MASTER CHECK ...`) and dispatch resumes once it is green or unchecked. |
| `LIMITED <slug> lane=codex until 22:55 (attempt not counted)` | Codex hit its usage limit. Its tasks move to the Claude lane until then (`lane_fallback`), unless the task says `Lane-Fixed: yes`. Copilot's premium-request / AI-credit limits do the same (`copilot -> claude`). | Nothing; it retries after the time shown. Buying more credit is your call. |
| `LANE_MISSING: codex not on PATH` | The scheduled task can't find the Codex CLI. | Check `codex` runs in a fresh terminal; reinstall it globally with npm if not. |
| `NO_REPORT … no report written` | The session ended without a report (crash, killed, lane failed to start). One retry, then parked. | Read `.review/reports/<slug>.lane.log`; fix the cause, then `requeue.py`. |
| Spend in the hundreds with `(estimated)`, then `daily spend cap $… reached` in `status.py` | The cost of a run killed or unreadable was estimated from minutes. On 2 Nov a parsing bug estimated runs at $289 and $665 and tripped the cap. | Check the real figure in the lane log. The parsing is fixed; the cap resets at midnight, or raise `daily_spend_cap_usd`. Codex, Gemini and Copilot runs (`unpriced_lanes`) never count against it and are never estimated. |
| `merge review/<slug>: merge conflict; merge by hand; follow-up <slug>-merge queued` | The branch conflicts with master (often several tasks editing `orchestra/runner.py` at once). The runner has already queued a merge-only task (`orchestra/conflicts.py`). | Nothing. If `<slug>-merge` parks too, read its report; then `requeue.py <slug>-merge --note "..."` or merge by hand. Without the follow-up note (`conflict_followup` off): `requeue.py <slug> --merge-only`. |
| `held <slug>: overlaps <other> on orchestra/runner.py (hold 1/2)` | A runnable task was passed over because it probably edits a hot file another running task is editing. | Nothing: it runs as soon as the other finishes, or anyway after `conflict_max_holds` holds. `conflict_hot_files` in `config.json` is the list. |
| `PARKED … session said PASS, but files changed in the main checkout during the run` | Someone (you, or a chat) edited the main checkout while it ran. | If the edit was unrelated, requeue with a note to merge the kept branch. |
| `integration … FAIL … run_checks --fast … exited 3221225786` | The combined-tree check was interrupted (0xC000013A, Ctrl+C or a closed window), not a real failure. | Requeue with a note to cherry-pick the kept branch and report PASS if the gate passes. |
| `integration … FAIL: gate: fail … FAILED (errors=9)` | The branch passed on its own but fails on current master. If both sides changed the same files the park reason ends `follow-up <slug>-merge queued` and a merge-only task is already in the queue. | With a follow-up: nothing, unless it parks too. Without one: requeue with a note naming what changed on master. |
| `AUTO-REQUEUE <slug>: reviewer <lane> free` | A PASS that had parked only because no reviewer could run was re-queued merge-only by the runner itself (`auto_requeue_review_*`). | Nothing. `AUTO-REQUEUE <slug>: stopped after N attempt(s); left parked` means it gave up: read the reviewer log, then `requeue.py <slug> --merge-only` by hand. |
| `master moved while testing` | Another merge landed during the combined-tree test. | Nothing: it retries once, then parks as "master moved twice, retry". |
| `broken gate` / `exited 5` / `exited 127` | The gate found no tests or a command doesn't exist. Never merged. | Fix the gate in the task (right path, right module), then requeue. |
| `run_checks --fast: skipped: … has no --fast option` | An older checkout without `--fast`. | Nothing now; it exists on master. |
| `push … not pushed: an added line looks like a key or token` | The privacy check refused the push. The merge stands locally. | Look at the outgoing diff (`git log origin/master..master -p`). If it's a false alarm, push by hand from the main checkout. |
| `worktree failed for <slug>: … exit status 3221225794` | `git worktree add` crashed at start-up (Windows, 0xC0000142). | Usually transient; the next tick retries. |
| `PARTIAL` / `FAIL … a blind re-run won't change it` | The session couldn't finish. | Read the report's NOT DONE and ODD lines, then requeue with a note that changes the task. |
| `TICK CRASHED: <Error>: ...` with an indented traceback | The runner itself raised. Before 3 Nov 2026 this was silent (pythonw has no stderr) and the task was dispatched again at once. If a task was running, its state says `RUNNER_CRASH`: one retry, then parked. | Read the traceback, fix the runner, requeue. |
| `RUNNER CRASHED <slug>: <Error> during review\|integration after the session's PASS` then `PARKED` | The session's work is done and on `review/<slug>`; the runner failed after it. This is what killed the 2 Nov `multistep-read-found-file` run (a curly quote in the diff under the cp1252 console code page). | Fix the cause, then `requeue.py <slug> --merge-only`. Nothing is lost. |
| `config: <key> should be a number; using the default …` (or `is not a valid regex`, `unreadable`) | A value in `orchestra/config.json` has the wrong shape; the runner is using the fallback from `config_defaults.json`. | Fix the value in `config.json`; the line stops on the next tick. |
| `<name>.lock: holder <pid> is gone; taken over` / `RECONCILE <name>.lock: holder <pid> is gone; removed` | A tick died holding a brief lock (state, merge, dispatch, reconcile). | Nothing: the next tick takes it over or sweeps it. |
| `could not remove <name>: still open in another process` | Windows refused to delete a lock another tick was reading for a whole second. | Nothing; the next tick retries. If it repeats, look for a stuck process holding `.review/` files open. |
| `integration <slug>: merge <sha> already on master; recovering` | A tick died between its fast-forward and its state save; the merge was on master but unrecorded and unpushed. | Nothing: it is recorded as `recovered:` and pushed. |
