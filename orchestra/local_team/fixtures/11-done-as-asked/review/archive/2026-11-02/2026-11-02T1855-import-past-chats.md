# Import the owner's past AI chats into the vault as Calendar/Chats notes
Queue: auto
Priority: normal
Weight: heavy
Kind: work
Lane: codex
Written: 2026-11-02T18:55

the owner wants every past AI conversation remembered in his Obsidian vault (<private path>), so he can keep a single Claude chat. Build the importer and its tests; do NOT run it against the real vault or his real session folders (the runner's tests and the owner's review come first).

Add `atrium/import_chats.py` (stdlib only), run as `python -m atrium.import_chats --source {claude-code,codex,claude-export} --path <dir or file> --vault <vault> [--apply]`. Dry run by default: print what it would write.
- claude-code: every `*.jsonl` under `<path>` (default `~/.claude/projects`). Reuse `atrium.capture.digest()` / `render()` where they fit.
- codex: every `*.jsonl` under `<path>` (default `~/.codex/sessions`). Read its event lines; take user and assistant messages only.
- claude-export: the `conversations.json` from a claude.ai data export (Settings → Privacy → Export data). Each conversation has a name, created_at and chat_messages with sender human/assistant and text.

One note per conversation at `Calendar/Chats/<YYYY-MM-DD> <source> <short title>.md` (title sanitised for Windows, max 60 chars). Frontmatter: name (unique, = file stem), description (one line: what the chat was about, from the first user message), updated (today), type: chat, source, date, tags: [chat, <source>]. Body: a "What was asked" list (each user message, trimmed to ~300 chars), "Outcome" (the last assistant message, trimmed to ~800 chars), and the original file path or conversation id. Never copy whole transcripts. Skip conversations with no user text. Re-running must not duplicate: keep `<vault>/Dev/chat-import-state.json` mapping source id -> note path and skip those already imported.

Privacy: drop any line that looks like a password, API key or token (reuse the patterns in `orchestra/aftercare.py` SECRET_TEXT, plus lines containing "password:" or "passwd"). Never write outside `<vault>/Calendar/Chats/` and that one state file.

Also add `Calendar/Chats` to `scripts/vault_contract.py` wherever generated folders are listed (like `Calendar/Handoff-`), if needed for the contract to pass on these notes.

Tests in `atrium/test_import_chats.py` with temp dirs and small fake inputs for all three sources: notes written with valid frontmatter, a second run writes nothing new, a fake key is not copied, nothing written in dry run.

## Gate
```
.venv\Scripts\python.exe -m unittest atrium.test_import_chats -v
.venv\Scripts\python.exe -m unittest atrium.test_atrium -v
```
