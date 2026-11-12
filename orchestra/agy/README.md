# agy lane settings

Session settings for the Google Antigravity CLI (agy), passed with --settings; agy rejects unknown keys, so the reasons live here.

- coder_settings.json: Session settings for the Antigravity CLI (agy) when Orchestra runs it as a coding lane (Chuen, 12 Nov 2026: Gemini in Orchestra, without auto-approving every tool). Passed with --settings <this file>; never saved into agy's own settings. Precedence in agy is Deny > Ask > Allow, and in headless print mode anything that would Ask is refused. Workspace = the task's own worktree. Commands still run inside agy's terminal sandbox (workspace only, no network) under --mode auto.
- reviewer_settings.json: Session settings for agy as a read-only reviewer (Chuen, 12 Nov 2026). Reads the worktree and git history only; every write, URL, MCP tool and any other command is denied. Deny > Ask > Allow.
