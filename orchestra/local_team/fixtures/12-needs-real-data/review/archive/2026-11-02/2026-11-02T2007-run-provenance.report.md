RESULT: PASS
GATE: `.venv\Scripts\python.exe -m unittest orchestra.test_runner -v` -> 50 tests OK, exit 0; `.venv\Scripts\python.exe orchestra\status.py --by-prompt` -> exit 0 (prints "no runs", because this worktree has no state.json). Also ran test_aftercare, test_integration, test_supervision, test_tools and test_install_vault: 37 OK. Does not cover: a real claude/codex lane run (the fake lane imitates claude's result JSON, copied from a real lane log); --by-prompt against the real .review/state.json (the guard refused even a read-only run aimed at the main checkout); the vault note through write_vault_note in the real vault (aftercare.vault_note is tested directly in a temp dir).
HUMAN: none
PREMISE: right (the task-file `digest` is SHA-1 at runner.py:214, not ~189. Nothing recorded the prompt version, base commit, model or tokens.)
DONE:
- runner.py: every history entry (normal, LIMITED and reconciled) now has `prompt_sha` (first 12 hex of SHA-256 of the rendered prompt that was sent), `prompt_file_sha` (review_prompt.md), `base_commit` (the worktree's HEAD at start), `lane`, `model` (the modelUsage key with the highest cost, or null), `tokens` {in, out, cache_read, cache_write} (null when the lane reports no usage; a key the usage lacks is null, not 0) and `session_id`. They are written through the existing finish() -> save_state() path.
- run_lane also puts the runner-side fields into the lock, so a run that reconcile() collects after a killed tick keeps its provenance.
- The result log line now ends `prompt <sha> tokens in .. out .. cache-read .. cache-write ..` (or `tokens n/a`). status.py's log tail shows it, and status.py has a new "Recent runs (last 5)" block with lane, model, prompt and tokens.
- aftercare.vault_note: the `- Task:` line now reads `` `slug` · prompt `<sha>` · tokens ... ``. write_vault_note passes in the last history entry's values.
- `status.py --by-prompt` (also works with `--json`): PASS / PARTIAL / FAIL / NO_REPORT counts, an `other` count (TIMEOUT, LIMITED), runs and median minutes per prompt_file_sha over the last 14 days. Runs from before this change show as `(unrecorded)`.
- 5 new tests in orchestra/test_runner.py: fields recorded from the fake lane's JSON (including the hash checks and base commit); missing or partial usage gives nulls, not zeros; a plain-text Codex log gives {}; a reconciled run keeps the lock's provenance; the vault note shows prompt and tokens; --by-prompt grouping, the 14-day window, the median, and the CLI exiting 0 with and without state.
NOT DONE:
- Old history entries are not backfilled. Their prompt version can't be recovered.
ODD:
- `prompt_sha` will be the same for nearly every run. The rendered prompt only differs from the template by the stripped frontmatter and the fixed `.review/task.md .review/report.md` arguments, so it changes exactly when review_prompt.md does. I hashed what is actually sent, as the task asked; `prompt_file_sha` is the grouping key.
- Both hashes normalise CRLF to LF, so a CRLF checkout doesn't show up as a new prompt version.
- When modelUsage lists more than one model (e.g. a small helper model), `model` is the one with the highest cost.
- The token format helper is in both runner.py (fmt_tokens) and aftercare.py (_tokens). aftercare imports nothing from runner, and I kept it that way.
- The guard hook refused a read-only `status.py --by-prompt` run with ORCHESTRA_REPO pointed at the main checkout. I did not retry it.
- The new reconcile test creates .review/reports and parked itself, because tick() normally creates them before reconcile() runs.
COMMITS: 09d7e0f
