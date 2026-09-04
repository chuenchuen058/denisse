# Denisse — working notes for Claude

Denisse is Chuen's local Jarvis-style assistant: a Python system-tray app on
a local Ollama model (`llama3.1:8b`), no cloud LLM calls. `tray.py` is the
real UI — it runs headless under `pythonw.exe` with no persistent window.

## Read first

The durable memory lives in the Obsidian vault:

`C:\Users\Xichu\OneDrive - King's College London\Denisse\Memory\MEMORY.md`

Read that index, then the notes relevant to the task. In particular read
`denisse_conversation_memory.md` before touching conversation history,
`replay()`, or the noise gate — it records a bug that took three hypotheses
to pin down, and the two that were disproved.

## Ground rules for this repo

- **Re-read before editing.** Chuen runs his own Claude Code terminal
  sessions against this repo in parallel with Cowork sessions. Files change
  under you. Never `git add -A` — stage only what you changed.
- **`conversation.py` owns the history**, not `memory_store` directly.
  `replay()` = what the model sees (recent, prose only). `record()` = what
  goes on disk (everything, appended). `get_brain()` = one Brain per process.
- **The context window is 4096 tokens.** What goes into the prompt matters;
  this is where the 2026-09-04 bug came from.
- **Test against the real system before claiming a fix.** `scripts/ask.py`
  runs one prompt through the real brain; `scripts/healthcheck.py --ask`
  checks every moving part; `scripts/test_conversation.py` is the regression
  test for the history layer.
- **Don't re-enable `read_claude_memory()`** in `brain.py` (feeding the vault
  back into Denisse's own prompt). Measured 0/4 clean replies with it, 4/4
  without. Needs new evidence, not a good argument.

## Environment

- venv python: `.venv\Scripts\python.exe`
- Every PowerShell call prints a red execution-policy error about
  `Microsoft.PowerShell_profile.ps1` first. **Noise, not failure.**
- The tray appears as two `pythonw.exe` processes and that is *one* tray —
  the venv launcher stub spawns the base interpreter.
- Multi-line commit messages: write to a temp file, `git commit -F` it.
- **No git remote.** Everything is on one machine.
