# Nightly: import new Claude Code and Codex sessions into the vault automatically
Queue: auto
Priority: normal
Weight: light
Kind: work
Lane: claude
Written: 2026-11-02T19:50

`atrium/import_chats.py` (merged today) turns Claude Code sessions (`~/.claude/projects`), Codex sessions (`~/.codex/sessions`) and a claude.ai export into `Calendar/Chats/` notes, idempotently via `Dev/chat-import-state.json`. the owner wants every conversation remembered without asking. Hook it into the nightly job (`scripts/nightly.py`; read how it gets the vault path and how it reports steps).

- New nightly step: import `claude-code` and `codex` sources with `--apply` into the real vault; skip sessions modified in the last 30 minutes (still running); cap at 200 notes per night; log counts. A failure in this step is logged and never stops the rest of the nightly.
- If a file named `claude-export*.zip` or `conversations.json` appears in `~/Downloads`, import it too (unzip to a temp dir), then record its hash so it's never imported twice. Never delete or move his download.
- Commit only the new `Calendar/Chats/*.md` notes and the state file in the vault repo, by path (never `git add -A`), message "Nightly chat import".
- Unit tests with temp dirs for: skip-recent, cap, export pickup, idempotency, commit only those paths (use a temp git repo).

Do not run the step against the real vault in this task.

Put new test modules in scripts/ next to the existing ones (the gate discovers them there).

## Gate
```
.venv\Scripts\python.exe -m unittest atrium.test_import_chats -v
.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_nightly_chat_import.py" -v
```
