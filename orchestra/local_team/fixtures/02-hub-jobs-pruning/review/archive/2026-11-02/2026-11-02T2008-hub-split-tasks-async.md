# Agent hub: split_tasks runs as a background job too, and old job files get pruned
Queue: auto
Priority: normal
Weight: light
Kind: work
Lane: claude
Written: 2026-11-02T20:08

Follow-up from the hub-async-jobs task (merged 2 Nov, report NOT DONE section): in `orchestra/agent_hub/server.mjs` every `ask_*`, `route_task` and `ask_all` now returns a job id at once, because the desktop bridge cancels any tool call over 60 s. `split_tasks` (~line 435) still `await`s up to 5 routed runs inline, so it is the one tool that can still be cut off at 60 s. Also `.review/hub/jobs/` grows forever (`jobs()` only filters to 24 h).

1. `split_tasks` returns one job (via the existing `asJob`) whose result is the joined outputs; per-sub-task failures appear in the text, and the job fails only if every sub-task failed (same rule as `ask_all`). Respect `MAX_JOBS`.
2. Prune job files (`<id>.json` and its output file) older than 7 days when the hub starts and at most once an hour after; never delete a job whose status is `running`. Env `AGENT_HUB_JOB_KEEP_DAYS` overrides.
3. Tests in `orchestra/agent_hub/test_hub.mjs` with `fake_agent.mjs`: split_tasks returns a job id immediately and its result contains every sub-task's output; pruning removes old finished jobs and keeps running/recent ones (temp `AGENT_HUB_JOBS_DIR`).

The agent-exams-router task (queued) will change `chooseRoute()`/`ROUTES` in the same file; touch only split_tasks and the job store.

## Gate
```
node --test orchestra/agent_hub/test_hub.mjs
```
