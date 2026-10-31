# <short title, what will be true when done>
Queue: draft
Priority: normal
Weight: light
Kind: work
Lane: claude
Written: 2026-10-31T16:00

<The facts the session needs: which files, what's wrong, what you want.
Write it so a session with no memory of today can do it. One job per file.
Put real paths in, relative to the repo root.>

## Gate
<One line saying what "worked" means, then the commands. The runner re-runs
every command in the block below inside the worktree before it merges, and
merges only if all exit 0. No command block = never auto-merged.>

```
.venv\Scripts\python.exe scripts\test_tasks.py
```
