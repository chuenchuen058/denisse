# Denisse

[![Offline regression checks](https://github.com/chuenchuen058/denisse/actions/workflows/checks.yml/badge.svg)](https://github.com/chuenchuen058/denisse/actions/workflows/checks.yml)

<p align="center"><img src="assets/orb.svg" width="140" alt="Denisse's animated purple orb"/></p>

Denisse is a Windows desktop assistant that runs its language model locally:
`llama3.1:8b` through Ollama, with an 8192-token context, on a laptop with an
8 GB GPU. It answers by text or voice from an animated orb and acts on the
machine (files, tasks, timers, apps). The engineering problem is not calling a
model; it is getting an 8B model to pick the right action, checking what
actually ran, and not reporting success that did not happen. Most of the code
is the deterministic scaffolding that does that, and the repository is built
to show, with reproducible checks, how often it works.

## What it does, and where the evidence is

| Claim | Evidence |
|---|---|
| Each request is offered a small, relevant set of tools rather than every schema | [`tool_router.py`](tool_router.py), [`eval/test_router.py`](eval/test_router.py) |
| Tool calls run in a bounded loop with repair of malformed calls | [`brain.py`](brain.py), [`scripts/test_tool_args.py`](scripts/test_tool_args.py) |
| Replies that claim an action without tool evidence are nudged once, then replaced | [`scripts/test_reply_guards.py`](scripts/test_reply_guards.py), [`scripts/test_brain_guards.py`](scripts/test_brain_guards.py) |
| History on disk keeps everything; the model sees only recent prose | [`conversation.py`](conversation.py), [`scripts/test_conversation.py`](scripts/test_conversation.py) |
| Accepted corrections can change routing; this does not train weights | [docs/adaptive_brain.md](docs/adaptive_brain.md), [`scripts/test_adaptive_brain.py`](scripts/test_adaptive_brain.py) |
| Standing jobs (task review, project status, Downloads rules) run unattended and stay in scope | [docs/standing_instructions.md](docs/standing_instructions.md), [`scripts/test_standing_instructions.py`](scripts/test_standing_instructions.py) |
| Offline speech recognition, interruptible speech, attached desktop chat | [docs/pet_chat.md](docs/pet_chat.md), [docs/voice_latency.md](docs/voice_latency.md), [`scripts/test_pet_chat.py`](scripts/test_pet_chat.py) |
| Repeated real-model runs with pass^k, recorded with machine and commit | [docs/denisse_evidence.md](docs/denisse_evidence.md), [`demo/results/`](demo/results/) |

Architecture, with a diagram of the request path and the build system:
[docs/architecture.md](docs/architecture.md).

## How it is built and tested

Most of this code is written by coding agents run unattended through
**Orchestra** ([`orchestra/`](orchestra/README.md)):

- A task is a Markdown file with a `## Gate` block of shell commands. The
  runner gives each task its own git worktree and branch and runs an agent
  (Claude Code or Codex) under a guard hook that refuses pushes, writes
  outside the worktree and destructive git commands.
- The runner re-runs the gate itself; the agent's own report does not merge
  anything.
- Diffs that touch the runner, hooks or installers, or are large, go to a
  separate read-only reviewer agent first.
- The branch is then merged onto current `master` in a scratch worktree and
  the gate plus the offline suite are run on that combined tree. `master` is
  fast-forwarded to exactly the commit that was tested; a red combined tree
  parks the task.
- Anything involving money, credentials, sending in the owner's name or an
  irreversible choice is parked for a human.

Testing has three layers, and no model grades another model:

1. **Offline regression checks** ([`scripts/run_checks.py`](scripts/run_checks.py)):
   every test module in its own process, with a scratch vault and rules file.
   A sandbox fails any test that reaches the real vault, personal memory or the
   network (Ollama included). Runs on every push and pull request in the
   [Offline regression checks](https://github.com/chuenchuen058/denisse/actions/workflows/checks.yml)
   workflow (badge above).
2. **Real-model evals** ([`eval/run_eval.py`](eval/run_eval.py)): the prompt
   suite through the actual local model, run nightly.
3. **Repeated runs** ([`demo/run_demo.py`](demo/run_demo.py)): the suite and
   scenario evals repeated k times, recorded with machine, model and commit.

Day-to-day operation: [docs/operator_guide.md](docs/operator_guide.md).
Security model of the runner: [docs/orchestra_security.md](docs/orchestra_security.md).

## Measured results

From [`demo/results/2026-11-02.json`](demo/results/2026-11-02.json)
(llama3.1:8b, Q4_K_M, RTX 5070 Laptop GPU with 8 GB). Each case was attempted
k times; pass^k is the mean chance that all k attempts of a case pass. Full
breakdown and failure reasons: [docs/denisse_evidence.md](docs/denisse_evidence.md).

| Eval (`parts.<name>.summary`) | Cases (`cases`) | k (`k`) | Passed attempts (`passes` / `attempts`) | Cases passing all k (`cases_all_pass`) | pass^k (`pass_hat_k`) |
|---|---|---|---|---|---|
| Prompt suite (`suite`) | 57 | 3 | 171 / 171 | 57 | 1.0 |
| Multi-turn dialogue (`dialogue`) | 7 | 3 | 20 / 21 | 6 | 0.857 |
| Unattended action loop (`actions`) | 4 | 3 | 12 / 12 | 4 | 1.0 |
| Feedback learning off (`learning.learning_off`) | 3 | 3 | 6 / 9 | 2 | 0.667 |
| Feedback learning on (`learning.learning_on`) | 3 | 3 | 9 / 9 | 3 | 1.0 |

`scripts/test_readme_claims.py` checks that every number in this table equals
the cited file and that the file is the latest in `demo/results/`.

## Limitations

- The eval sets are small, hand-written and run on one laptop. They catch
  regressions and show repeatability; they are not a general accuracy score.
- Checks are deterministic (tools called, required or forbidden text,
  invented paths). Reply quality beyond those checks is not measured.
- The voice stack was not measured in this run (Kokoro model files were not
  present), and end-to-end voice latency and real-room recognition are not
  measured at all.
- The local model still makes wrong tool choices and weak conversational
  replies, as the dialogue row above shows.
- Typed shell commands, file writes and app opening run without a
  confirmation prompt, following the original user's configuration;
  hands-free mode asks first. See [`safety.py`](safety.py). This is not a
  sandbox for untrusted instructions, and cancelling speech does not undo an
  action that already ran.
- Optional speech synthesis (Inworld), Spotify, news and weather use network
  services. Text chat and speech recognition do not.
- Windows only, Python 3.12 only (the audio code uses `audioop`, removed in
  3.13). This is a personal prototype, not a product.

## Quick start

Windows and Python 3.12:

```powershell
git clone https://github.com/chuenchuen058/denisse.git
cd denisse
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run the offline checks (no model, microphone or API keys needed):

```powershell
.venv\Scripts\python.exe scripts/run_checks.py --report logs/checks.json
```

Text chat needs [Ollama](https://ollama.com) running:

```powershell
ollama pull llama3.1:8b
.venv\Scripts\python.exe main.py
```

`DENISSE_MODEL` and `DENISSE_NUM_CTX` are optional; see
[.env.example](.env.example).

Desktop orb (Ctrl+Alt+D opens the chat from any app):

```powershell
.venv\Scripts\python.exe tray.py
```

The tray also starts Downloads sorting and temporary-file maintenance, which
can be paused from its menu. Use the text CLI first if you would rather not
enable those.

Offline voice (Vosk model downloaded once; `voice_mode.py` is push-to-talk,
hands-free is in the tray or `wake_word.py`):

```powershell
.venv\Scripts\python.exe scripts/download_vosk_model.py
.venv\Scripts\python.exe voice_mode.py
```

Optional pieces, each documented separately:

- Standing jobs: `scripts/standing.py --init --enable`, see
  [docs/standing_instructions.md](docs/standing_instructions.md).
- Preferences and an Obsidian vault (`DENISSE_VAULT`): `scripts/brain_settings.py --init`,
  see [docs/adaptive_brain.md](docs/adaptive_brain.md); template in
  [brain_rules.example.json](brain_rules.example.json).
- Chat controls, hotkey and stopping speech: [docs/pet_chat.md](docs/pet_chat.md).
- Reproducing the measured results: [docs/denisse_evidence.md](docs/denisse_evidence.md#reproduce).

Generated reports, downloaded models, credentials and personal memory stay
out of Git.
