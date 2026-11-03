RESULT: PASS
GATE: `.venv\Scripts\python.exe -m unittest atrium.test_import_chats -v` (10 tests OK, exit 0); `.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_nightly_chat_import.py" -v` (14 tests OK, exit 0) -> pass; does not cover: a real run of scripts/nightly.py (nightly.py itself only parse-checked, by design: the task says not to touch the real vault), real ~/.claude and ~/.codex data volume and runtime, OneDrive/real-vault git behaviour, or the private backup push of the new commit
HUMAN: none
PREMISE: right (atrium/import_chats.py exists with claude-code/codex/claude-export sources and Dev/chat-import-state.json; nightly.py hard-codes VAULT and reports through say())
DONE:
- atrium/import_chats.py: `import_chats(..., limit=, settled_before=)`. limit caps how many notes one run writes, and anything left over stays out of the state file so the next run picks it up. settled_before skips JSONL files modified at or after the cutoff. The CLI and default behaviour are unchanged.
- scripts/nightly_chat_import.py `run()`: imports claude-code and then codex with apply, skips files modified in the last 30 minutes, and caps the whole night at 200 notes. It also picks up `claude-export*.zip` or `conversations.json` from the top level of ~/Downloads. For a zip it extracts only conversations.json, under a fixed name, into a temp dir. It records `claude-export-file:<sha256>` in Dev/chat-import-state.json, and never moves or deletes the download. It commits only the new notes plus the state file through `vault_git.commit_owned` (literal pathspecs, `commit --only`), with the message "Nightly chat import". It logs counts for each source and never raises.
- scripts/nightly.py: new stage `== chats ==` (4b), before the keats and commit stages. `--dry` gives a dry run with no writes and no commit. The log trail now has `chats=` and `chat_failures=`.
- scripts/test_nightly_chat_import.py (14 tests, all on temp dirs or a temp git repo): skip-recent; cap and backlog draining; zip export picked up once with the download untouched; plain conversations.json; an export still downloading; a capped export whose hash isn't recorded until it finishes; idempotency across nights; one source failing while the others continue, with no exception text logged; bad state; missing directories; dry run; commit contains only the new notes and the state file (other staged work, untracked files and old notes are kept, and there's no empty commit on the next night); commit failure is reported, not raised.
NOT DONE:
- Did not run the step against the real vault, as instructed.
ODD:
- I put the export hash in the same Dev/chat-import-state.json under a `claude-export-file:` key. That keeps one state file, it gets committed with the notes, and the importer's str->str check still passes.
- Exports in Downloads modified in the last 30 minutes are skipped too (they may still be downloading). If the cap cuts an export off partway, its hash is left unrecorded so the next night finishes it.
- If the chat commit fails, nightly's existing `git_error` is set, so the job still runs every stage but exits 1 at the end. That matches how the stage-5 commit failure is already handled.
- The cap fills claude-code first, then codex, then exports. A large first-night backlog of Claude Code sessions will hold back codex for a few nights until it drains.
- Every night still parses every JSONL under ~/.claude/projects, because the state is keyed by session ID and not by file. This is fine at the current size but could get slow as the history grows. It includes subagent and orchestra-worktree sessions, since the importer has no filter for them.
- Logged errors show only the exception type, because the message text could quote private chat content.
COMMITS: e252f0c
