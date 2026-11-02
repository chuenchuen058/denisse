# Operator's guide: Orchestra and Atrium

One page for running the thing day to day. The detail behind each part lives in
[`orchestra/README.md`](../orchestra/README.md) (every command and mechanism),
[`.review/README.md`](../.review/README.md) (the queue folders) and
[`docs/orchestra_security.md`](orchestra_security.md) (the safety layers). Limits and
their reasons are in [`orchestra/config.json`](../orchestra/config.json).

Every command below is run from the repo root (`C:\Users\Xichu\GitHub\denisse`) with the
venv python. `orchestra/test_docs.py` checks that every script path in this guide's
command blocks exists.

## What runs where, and when

| Thing | Where it runs | When |
|---|---|---|
| **Runner** (`orchestra/runner.py`) | Task Scheduler task "Denisse Orchestra", from the main checkout | Every 2 minutes, **only while you are logged on** (no stored password). Screen locked is fine; logged off or asleep means no ticks. |
| **Task sessions** | Each in its own worktree, `..\denisse-wt\<slug>`, on branch `review/<slug>` | Started by a tick. Up to 3 Claude and 1 Codex at once (`lane_slots`). |
| **Power** | The runner checks the battery itself (`orchestra/power.py`) | On battery, ticks still run and finish off completed work (gate, merge, push), but **no new task starts** until you are back on mains. Running tasks are never killed for power. |
| **Producer** (local team, optional) | Task "Denisse Orchestra Producer", local Ollama model | Every 30 min if registered. Writes draft tasks only. |
| **Canary** (optional) | Task "Denisse Agent Canary" | Daily 07:10 if registered. One short exam per agent. |
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
```

`status.py` is the one screen: running tasks per lane slot, the queue and why each task
is waiting, parked tasks and why, today's results and spend, the last log lines.
`watch.py` follows the running sessions live. `runner.py --dry-run` shows what the next
tick would pick without running anything.

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
edit `state.json` by hand.

Pause, resume and stop (PowerShell; there is no Orchestra command for these yet):

```commands
Disable-ScheduledTask -TaskName "Denisse Orchestra"
Enable-ScheduledTask -TaskName "Denisse Orchestra"
Stop-ScheduledTask -TaskName "Denisse Orchestra"
Get-ScheduledTask -TaskName "Denisse Orchestra"
```

- **Pause** = disable the task. Running sessions finish, but nothing collects them until
  you resume; the next tick then gates and merges them.
- **Resume** = enable it. The next tick reconciles before it starts anything new.
- **Stop now** = stop the running tick. Each session runs inside a Windows Job Object, so
  ending the runner ends the whole session tree with it. The next tick finds the dead lock
  and collects whatever report exists (none: one infra retry).
- **Hold one task** = change its `Queue: auto` back to `Queue: draft`.

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
powershell -ExecutionPolicy Bypass -File orchestra\install_task.ps1
```

`atrium.import_chats` is a dry run until you add `--apply`. `install_task.ps1`
re-registers "Denisse Orchestra" in place (add `-Canary` / `-Producer` for those tasks).

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
| `LIMITED <slug> lane=codex until 22:55 (attempt not counted)` | Codex hit its usage limit. Its tasks move to the Claude lane until then (`lane_fallback`), unless the task says `Lane-Fixed: yes`. | Nothing; it retries after the time shown. Buying more credit is your call. |
| `LANE_MISSING: codex not on PATH` | The scheduled task can't find the Codex CLI. | Check `codex` runs in a fresh terminal; reinstall it globally with npm if not. |
| `NO_REPORT … no report written` | The session ended without a report (crash, killed, lane failed to start). One retry, then parked. | Read `.review/reports/<slug>.lane.log`; fix the cause, then `requeue.py`. |
| Spend in the hundreds with `(estimated)`, then `daily spend cap $… reached` in `status.py` | The cost of a run killed or unreadable was estimated from minutes. On 2 Nov a parsing bug estimated runs at $289 and $665 and tripped the cap. | Check the real figure in the lane log. The parsing is fixed; the cap resets at midnight, or raise `daily_spend_cap_usd`. Codex runs never count against it. |
| `merge review/<slug>: merge conflict; merge by hand` | The branch conflicts with master (often several tasks editing `orchestra/runner.py` at once). | `requeue.py <slug> --note "merge review/<slug> onto your fresh branch, keep both sides, re-run the gate"`. |
| `PARKED … session said PASS, but files changed in the main checkout during the run` | Someone (you, or a chat) edited the main checkout while it ran. | If the edit was unrelated, requeue with a note to merge the kept branch. |
| `integration … FAIL … run_checks --fast … exited 3221225786` | The combined-tree check was interrupted (0xC000013A, Ctrl+C or a closed window), not a real failure. | Requeue with a note to cherry-pick the kept branch and report PASS if the gate passes. |
| `integration … FAIL: gate: fail … FAILED (errors=9)` | The branch passed on its own but fails on current master. | Requeue with a note naming what changed on master. |
| `master moved while testing` | Another merge landed during the combined-tree test. | Nothing: it retries once, then parks as "master moved twice, retry". |
| `broken gate` / `exited 5` / `exited 127` | The gate found no tests or a command doesn't exist. Never merged. | Fix the gate in the task (right path, right module), then requeue. |
| `run_checks --fast: skipped: … has no --fast option` | An older checkout without `--fast`. | Nothing now; it exists on master. |
| `push … not pushed: an added line looks like a key or token` | The privacy check refused the push. The merge stands locally. | Look at the outgoing diff (`git log origin/master..master -p`). If it's a false alarm, push by hand from the main checkout. |
| `worktree failed for <slug>: … exit status 3221225794` | `git worktree add` crashed at start-up (Windows, 0xC0000142). | Usually transient; the next tick retries. |
| `PARTIAL` / `FAIL … a blind re-run won't change it` | The session couldn't finish. | Read the report's NOT DONE and ODD lines, then requeue with a note that changes the task. |
