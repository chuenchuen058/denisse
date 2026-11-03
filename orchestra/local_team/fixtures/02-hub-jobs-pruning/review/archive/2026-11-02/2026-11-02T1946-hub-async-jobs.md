# Agent hub: long jobs as background jobs, so the 60-second bridge timeout stops cancelling them
Queue: auto
Priority: urgent
Weight: heavy
Kind: work
Lane: claude
Written: 2026-11-02T19:46

`orchestra/agent_hub/server.mjs` is the MCP server Claude (Cowork) uses to ask Codex, Copilot and Gemini. The desktop bridge cancels any tool call that takes longer than 60 s, so a real `ask_codex` (often 1-5 min) is lost mid-run and its output thrown away.

Add background jobs:
- `ask_codex`, `ask_copilot`, `ask_gemini`, `ask_all` and `route_task` take an optional `wait_s` (default 45). If the agent finishes within it, return the answer as today. If not, return `{job_id, status: "running", agent, started}` and keep the child process running.
- New tools `job_status(job_id)` (running/done/failed, elapsed, last 20 lines of output) and `job_result(job_id)` (the full answer, plus the same FOR MEMORY handling into `Calendar/Handoff-<date>.md` that a direct call does — exactly once per job), and `jobs()` listing jobs from the last 24 h.
- Jobs persist to `.review/hub/jobs/<id>.json` + `<id>.out` so a hub restart doesn't lose finished results; a job whose process died with the hub is marked failed with that reason.
- Hard cap: 4 jobs running at once, a per-job timeout (default 15 min, configurable), and kill-tree on timeout (Windows: `taskkill /T /F /PID`).
- Keep the existing FALLBACK behaviour (gemini/copilot -> codex) working for jobs.
- Update the tool descriptions so Claude knows to poll with `job_status` rather than re-asking.

Tests in `orchestra/agent_hub/test_hub.mjs` with a fake agent command (a node script that sleeps then prints) covering: fast path returns inline, slow path returns a job then a result, persistence across a simulated restart, the 4-job cap, timeout kill, FOR MEMORY written once.

## Gate
```
node --test orchestra/agent_hub/test_hub.mjs
```
