# Orchestra and Atrium

[![Offline regression checks](https://github.com/chuenchuen058/denisse/actions/workflows/checks.yml/badge.svg)](https://github.com/chuenchuen058/denisse/actions/workflows/checks.yml)

A one-laptop system that runs several coding agents unattended and keeps them
honest. **Orchestra** hands tasks to Claude Code, Codex, Copilot and Gemini,
has a different agent review risky changes, and merges nothing the tests have
not passed. **Atrium** is the shared memory and dashboard around it: one
handoff log and context pack every agent reads, session capture into an
Obsidian vault, and a live page showing what each agent is doing.

The engineering problem is not calling a model. It is letting agents work
overnight without supervision while making sure a wrong, unsafe or untested
change never lands, and that a person can see at a glance what happened.
Most of the code is the deterministic scaffolding that does that.

## Orchestra: the task runner

[`orchestra/`](orchestra/README.md) runs from Windows Task Scheduler every two
minutes. Each tick picks the top runnable task and moves it one step.

1. **A task is a Markdown file** in `.review/queue/` with a `## Gate` block of
   shell commands that must pass ([`orchestra/new_task.py`](orchestra/new_task.py)).
2. **The runner picks the lane itself** from each agent's measured strengths,
   current limits and cost ([`orchestra/lane_pick.py`](orchestra/lane_pick.py)),
   and tracks every lane's usage window and reset time
   ([`orchestra/allowance.py`](orchestra/allowance.py)).
3. **The agent works in its own git worktree and branch**, under a guard hook
   that refuses pushes, writes outside the worktree and destructive git
   commands ([docs/orchestra_security.md](docs/orchestra_security.md)).
4. **The runner re-runs the gate itself.** The agent's own report never merges
   anything.
5. **Risky diffs go to a different agent** as a read-only reviewer. A block
   gets one automatic fix round with the findings as the note, unless it names
   a forbidden area or says the task's premise is wrong
   ([`orchestra/runner.py`](orchestra/runner.py)).
6. **Integration on a scratch tree.** The branch is merged onto current
   `master`, the gate and the offline suite run on that combined tree, and
   `master` is fast-forwarded to exactly the commit that was tested. A red
   combined tree parks the task with the reason.
7. **People decide the irreversible things.** Anything involving money,
   credentials, sending in the owner's name or a one-way choice is parked for
   a human ([`orchestra/unattended.py`](orchestra/unattended.py)).

Around the runner: a daily spend cap and campaign controls
([`orchestra/campaign.py`](orchestra/campaign.py)), conflict-aware dispatch
([`orchestra/conflicts.py`](orchestra/conflicts.py)), a local producer team on
Ollama that proposes tasks, with a no-model admission layer deciding what
enters the queue ([`orchestra/admission.py`](orchestra/admission.py)), daily
agent exams that score each lane ([`orchestra/exams/`](orchestra/exams/)), and a
soak test that runs the whole runner against fake lanes with kills and power
flips ([docs/orchestra_soak.md](docs/orchestra_soak.md)).

Day-to-day operation: [docs/operator_guide.md](docs/operator_guide.md).

## Atrium: shared memory and the live page

[`atrium/`](atrium/) gives every agent the same picture of what is going on:

- **One handoff log** that every agent appends tagged lines to, and **one
  context pack** each agent reads at the start of a session
  ([`atrium/handoff.py`](atrium/handoff.py), [`atrium/context.py`](atrium/context.py)).
- **Session capture**: Claude Code sessions are saved into the vault by a hook,
  every 40,000 tokens and at session end ([`atrium/capture.py`](atrium/capture.py)).
- **A token ledger** of what every agent spent, per day, from every source on
  the laptop ([`atrium/usage_ledger.py`](atrium/usage_ledger.py)).
- **The live page** ([`atrium/live.py`](atrium/live.py)), served on the laptop's
  loopback address: a mission board with each agent's state, what is running
  and how long it has left, a cursor per agent showing where it is working and
  who is about to collide ([`atrium/cursors.py`](atrium/cursors.py)), today's
  results, the queue as a tree, and a Do next card
  ([`atrium/suggest.py`](atrium/suggest.py)). It updates over server-sent
  events when a watched file changes. Clicking an item runs it as a prompt in a
  new Claude Code window ([`atrium/run_claude.py`](atrium/run_claude.py)); the
  page's writes accept only loopback, same-origin JSON and, for that one, a
  per-run token.
- **A hub snapshot**: small JSON tables for a remote dashboard
  ([`atrium/snapshot.py`](atrium/snapshot.py)), and an hourly digest of the
  laptop into the vault ([`atrium/digest.py`](atrium/digest.py)).

Every limit, path, word and selector lives in
[`atrium/config.json`](atrium/config.json) and
[`orchestra/config.json`](orchestra/config.json), each with its reason. Tests
fail if a page file writes its own colours or text.

## How it is tested

No model grades another model:

1. **Offline regression checks** ([`scripts/run_checks.py`](scripts/run_checks.py)):
   every test module in its own process, with a scratch vault. A sandbox fails
   any test that reaches the real vault, personal memory or the network. Runs
   on every push in the
   [Offline regression checks](https://github.com/chuenchuen058/denisse/actions/workflows/checks.yml)
   workflow (badge above).
2. **The runner's own gate** on every task, and again on the combined tree
   before `master` moves.
3. **Real-model evals and repeated runs** of the local assistant
   ([`eval/run_eval.py`](eval/run_eval.py), [`demo/run_demo.py`](demo/run_demo.py)),
   recorded with machine, model and commit.

## The local assistant

The repository started as **Denisse**, a Windows desktop assistant that runs
`llama3.1:8b` locally through Ollama (8 GB GPU, 8192-token context) and acts on
the machine by text or voice. It is now one part of the system: Orchestra's
local agents and the study tutor run on the same local model. Its design and
evidence: [docs/architecture.md](docs/architecture.md),
[docs/denisse_evidence.md](docs/denisse_evidence.md).

## Measured results

The local assistant's evals, from
[`demo/results/2026-11-03.json`](demo/results/2026-11-03.json)
(llama3.1:8b, Q4_K_M, RTX 5070 Laptop GPU with 8 GB). Each case was attempted
k times; pass^k is the mean chance that all k attempts of a case pass. Full
breakdown and failure reasons: [docs/denisse_evidence.md](docs/denisse_evidence.md).

| Eval (`parts.<name>.summary`) | Cases (`cases`) | k (`k`) | Passed attempts (`passes` / `attempts`) | Cases passing all k (`cases_all_pass`) | pass^k (`pass_hat_k`) |
|---|---|---|---|---|---|
| Prompt suite (`suite`) | 83 | 3 | 242 / 249 | 80 | 0.964 |
| Multi-turn dialogue (`dialogue`) | 7 | 3 | 21 / 21 | 7 | 1.0 |
| Unattended action loop (`actions`) | 4 | 3 | 12 / 12 | 4 | 1.0 |
| Feedback learning off (`learning.learning_off`) | 3 | 3 | 6 / 9 | 2 | 0.667 |
| Feedback learning on (`learning.learning_on`) | 3 | 3 | 9 / 9 | 3 | 1.0 |

`scripts/test_readme_claims.py` checks that every number in this table equals
the cited file and that the file is the latest in `demo/results/`.

## Limitations

- Windows only, Python 3.12, built and run on one laptop. A personal system,
  not a product.
- Orchestra's safety rests on the guard hook, the runner's own gate and the
  integration check. A reviewer agent can miss a problem, and a gate is only as
  good as the tests it runs.
- The live page and its Run button are meant for the laptop's own browser on
  loopback; they are not built to be exposed on a network.
- The local evals are small, hand-written and run on one machine. They catch
  regressions; they are not a general accuracy score.

## Quick start

Windows and Python 3.12:

```powershell
git clone https://github.com/chuenchuen058/denisse.git
cd denisse
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe scripts/run_checks.py --report logs/checks.json
```

Orchestra (one tick, or a status screen):

```powershell
.venv\Scripts\python.exe orchestra\runner.py --dry-run
.venv\Scripts\python.exe orchestra\status.py
```

The live Atrium page:

```powershell
.venv\Scripts\python.exe -m atrium live --open
```

The local assistant needs [Ollama](https://ollama.com):

```powershell
ollama pull llama3.1:8b
.venv\Scripts\python.exe main.py
```

Generated reports, downloaded models, credentials and personal memory stay
out of Git.
