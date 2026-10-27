# AGENTS.md — Denisse

Instructions for any AI assistant working in this repo. Vendor-neutral;
`CLAUDE.md` alongside is a pointer to this file.

Denisse is Chuen's local Jarvis-style assistant: a Python system-tray app on
a local Ollama model (`llama3.1:8b`), no cloud LLM calls. `tray.py` is the
real UI — headless under `pythonw.exe`, no persistent window.

## Read first

Durable context lives in Chuen's Obsidian vault, not in this repo:

`C:\Users\Xichu\OneDrive - King's College London\Denisse\`

- `AGENTS.md` there — the vault contract
- `Memory\MEMORY.md` — who Chuen is, how he works, this machine
- `Denisse\Denisse.md` — this project's index
- `Denisse\roadmap.md` — where the project is going, and what is
  deliberately *not* being done (fine-tuning, cloud APIs) with reasons

Read `Denisse\denisse_conversation_memory.md` before touching conversation
history, `replay()`, or the noise gate. It records a bug that took three
hypotheses to pin down, and the two that were disproved.

## Ground rules

- **Re-read before editing.** Chuen runs his own terminal AI sessions
  against this repo in parallel with remote ones. Files change under you.
  Never `git add -A` — stage only what you changed.
- **Commit and push as work is completed.** Chuen requested this on
  2026-10-27. Make a focused commit after each meaningful, coherent change
  and its relevant checks, then push it; don't batch an entire session
  into one large commit. Keep code with the tests needed to verify it.
  Preserve other sessions' staged work and keep private vault/memory data
  out of this public repository. Never make empty commits just for counts.
- **`conversation.py` owns the history**, not `memory_store` directly.
  `replay()` = what the model sees (recent, prose only). `record()` = what
  goes on disk (everything, appended). `get_brain()` = one Brain per process.
- **Context is 8192 tokens** (`DENISSE_NUM_CTX`), raised from 4096 on
  2026-09-05 after measuring. Headroom on the 8GB GPU is ~2GB — don't raise
  it further without re-running `eval/run_eval.py`; a spill to CPU makes
  every reply dramatically slower.
- **Test before claiming a fix.** `scripts/ask.py` runs one prompt through
  the real brain; `scripts/healthcheck.py --ask` checks every moving part;
  `eval/run_eval.py` scores the prompt suite; `scripts/test_conversation.py`
  guards the history layer.
- **Don't re-enable `read_claude_memory()`** in `brain.py` (feeding the
  vault back into Denisse's own prompt). Measured 0/4 clean replies with it,
  4/4 without. Needs new evidence, not a good argument.

## Environment

- venv python: `.venv\Scripts\python.exe`
- Every PowerShell call prints a red execution-policy error about
  `Microsoft.PowerShell_profile.ps1` first. **Noise, not failure.** The same
  policy means `.ps1` files need `-ExecutionPolicy Bypass`.
- The tray appears as two `pythonw.exe` processes and that is *one* tray —
  the venv launcher stub spawns the base interpreter.
- Multi-line commit messages: write to a temp file, `git commit -F` it.
- Remote is `github.com/chuenchuen058/denisse` (public). Push your work.
