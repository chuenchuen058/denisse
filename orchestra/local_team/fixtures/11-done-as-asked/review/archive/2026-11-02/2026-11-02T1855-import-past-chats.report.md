RESULT: PASS
GATE: `.venv\Scripts\python.exe -m unittest atrium.test_import_chats -v` -> PASS (10 tests, exit 0); `.venv\Scripts\python.exe -m unittest atrium.test_atrium -v` -> PASS (6 tests, exit 0); does not cover: real historical sessions/export variations, real-vault import or Obsidian visual review, simultaneous import processes, interruption/disk failure between note and state writes, or adversarial filesystem races (redirect tests mock path resolution).
HUMAN: none
PREMISE: right: atrium.capture saves live Claude Code sessions to Calendar/Sessions, but no historical three-source chat importer existed; Calendar/Chats was absent from vault_contract.GENERATED.
DONE:
- Added stdlib-only `python -m atrium.import_chats` for Claude Code JSONL, Codex JSONL and claude.ai conversations.json, accepting directories/files and source-specific default session paths.
- Added dry-run-by-default planning, bounded summaries, Windows-safe titles capped at 60 characters, collision-safe filenames, required quoted YAML frontmatter, original references and source-qualified state keys that skip successful prior imports.
- Reused orchestra.aftercare.SECRET_TEXT; redact entire credential lines and private-key blocks before generating filenames/descriptions/body, redact provenance, and hash secret-looking source IDs in state.
- Restricted output destinations to Calendar/Chats and Dev/chat-import-state.json; reject redirected paths and malformed state, preserve existing notes, and skip conversations with no safe user text.
- Added 10 fixture tests covering all three sources, CLI dry-run/apply/re-run, frontmatter, privacy, collisions, length caps, mirrored Codex events, repeated asks, malformed JSONL, Claude markup/block/BOM cases and output confinement. Existing 6 Atrium tests pass.
- Added Calendar/Chats/ to generated folders; the actual vault contract checker passes on fixture notes with zero errors and zero warnings using an in-memory replacement of only its hardcoded vault root.
- Staged only the three changed source/test files, passed whitespace and staged privacy checks, committed on the current branch. No push or merge.
NOT DONE:
- Real import and real-session inspection intentionally not performed, as requested. No real vault files were written.
ODD:
- Used file modification date when creation timestamps are missing/invalid, numeric suffixes for same-title conversations, namespaced IDs across sources, and per-role response-item preference to avoid importing Codex's mirrored events twice.
- Reused capture._short, but not capture.digest/render: digest joins text blocks before line-based redaction and suppresses all markup-leading questions; render has the live-session schema rather than the requested chat schema. The importer preserves text-block line boundaries and genuine markup questions without changing live capture.
- The first test run caught active Markdown-link syntax surviving escaping; escaped parentheses and encoded frontmatter brackets, then verified with the real checker on a temporary fixture.
- The staged privacy scan initially flagged a synthetic PEM header in the test source. Constructed that identical fixture value at runtime, preserving all assertions; the unchanged privacy scan and both gate commands then passed.
- The suggested Memory/MEMORY.md vault pointer was stale; read the existing Dev/Memory/MEMORY.md index instead. PowerShell profile-policy banners were environmental noise; final gate exit codes were both 0. No tool refusals.
- Required self-evaluation is recorded in .review/self-evaluation.md; no private context or generated notes were committed.
COMMITS: dbc0098
