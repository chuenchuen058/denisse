# Orchestra: record what produced each result (prompt version, base commit, model, tokens)
Queue: auto
Priority: normal
Weight: light
Kind: work
Lane: claude
Written: 2026-11-02T20:07

Master prompt section 10 (`<private path>`): "Prompt templates are code-quality assets: version them ... and record which version produced each result." Today `orchestra/runner.py` hashes only the task file (`digest`, SHA-1, ~line 189). Nothing records which `orchestra/review_prompt.md` was used, the base commit the worktree started from, the model, or the token usage, so "did the new prompt make workers better?" can't be answered from data.

The claude lane's final JSON already contains what's needed (seen in `.review/reports/2026-11-02T1946-hub-async-jobs.lane.log`): `session_id`, `total_cost_usd`, `usage` (input/output/cache tokens), `modelUsage` keyed by model id. Stream-json lanes end with the same `result` event.

1. Per run, append to the task's history entry in state.json (through the existing locked save path, not by hand): `prompt_sha` (first 12 hex of SHA-256 of the rendered prompt text actually sent), `prompt_file_sha` (of review_prompt.md), `base_commit`, `lane`, `model` (from modelUsage, or null), `tokens` `{in, out, cache_read, cache_write}` (null when the lane doesn't report them — Codex, Gemini), `session_id`.
2. Show `prompt <sha>` and tokens in the vault's per-task Orchestra section (aftercare's existing writer) and in `orchestra/status.py`'s log lines. Tokens are telemetry, never presented as remaining allowance.
3. `python orchestra/status.py --by-prompt`: PASS / PARTIAL / FAIL / NO_REPORT counts and median minutes per `prompt_file_sha` over the last 14 days.
4. Tests in `orchestra/test_runner.py` (or a new `orchestra/test_provenance.py`): fields recorded from a fake lane's JSON; missing usage → nulls, not zeros; `--by-prompt` grouping.

Re-read runner.py on master before editing; other tasks touch it.

## Gate
```
.venv\Scripts\python.exe -m unittest orchestra.test_runner -v
.venv\Scripts\python.exe orchestra\status.py --by-prompt
```
