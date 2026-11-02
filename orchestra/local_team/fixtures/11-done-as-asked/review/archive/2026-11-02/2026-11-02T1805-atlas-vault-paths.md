# Point Denisse's code, tests and docs at the new Atlas vault layout
Queue: auto
Priority: urgent
Weight: heavy
Kind: work
Lane: codex
Written: 2026-11-02T18:05

the owner's Obsidian vault (<private path>) is being reorganised.
Top-level folders move (vault-relative):
  SEM 1 Modules -> Atlas/Academic/SEM 1 Modules
  Robotic Tail -> Atlas/Projects/Robotic Tail
  Projects -> Atlas/Projects/Other
  GitHub -> Atlas/Projects/GitHub
  LSEG Hackathon -> Atlas/Projects/LSEG Hackathon
  KCLMSoc -> Atlas/Clubs/KCLMSoc
  AKC -> Atlas/Clubs/AKC
  Internships -> Efforts/Internships
  Log -> Calendar/Log
  Memory -> Dev/Memory
  graphify-out -> Dev/graphify-out
  Panel neuroscience.md, Quotes.md -> Atlas/Concepts/Notes/
Unchanged: Denisse/, Atlas/, Efforts/, Calendar/, Dev/, Templates/, root notes.

Update every place in this repo's code, tests and docs that builds or names an OLD vault path to the NEW one.
Known places (search for others): scripts/nightly.py (MODULES + docstring), study_brief.py, planner.py,
vault_search.py (Memory/STATE.md -> Dev/Memory/STATE.md), obsidian_sync.py (CLAUDE_MEMORY_DIR),
scripts/vault_contract.py (mod_root, ROOTS, docstring "Memory/vault_setup.md", Log/Log.md comment),
scripts/qualify_attachments.py, scripts/test_planner.py, scripts/test_study_brief.py,
scripts/test_adaptive_brain.py, scripts/test_dialogue_reliability.py, scripts/eval_adaptive_brain.py,
AGENTS.md and CLAUDE.md (Memory\MEMORY.md -> Dev\Memory\MEMORY.md). Tests that build fake vaults build
them in the NEW layout. Do NOT change the repo's own lowercase memory/ folder (memory/conversation.json etc.).
Do NOT touch the vault folder itself and do NOT run .review/migrate_vault.py.

scripts/vault_theme.py colours the vault's top-level folders: change its folder list to the new six
(Atlas, Efforts, Calendar, Dev, Denisse, Templates), each a clearly different colour that fits its
existing purple-night palette, keeping its existing style and rules. Don't run it.

## Gate
The affected tests pass with the new layout.

```
.venv\Scripts\python.exe scripts\test_planner.py
.venv\Scripts\python.exe scripts\test_study_brief.py
.venv\Scripts\python.exe scripts\test_adaptive_brain.py
.venv\Scripts\python.exe scripts\test_dialogue_reliability.py
.venv\Scripts\python.exe scripts\test_obsidian_tasks.py
```

Re-queued 2 Nov 18:10: codex lane fixed (MCP servers off, full access).
Re-queued 18:25 (codex path fix).
