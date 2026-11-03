RESULT: PASS
GATE: node --test orchestra/agent_hub/test_hub.mjs -> exit 0, 16/16 pass (9 existing + 7 new); does not cover: real Codex/Gemini/Copilot CLIs (fakes only), the Claude desktop bridge actually honouring the inline-vs-job reply under its 60 s cut-off, a real hub process being killed mid-job (restart is simulated by a hand-written "running" record + a fresh module import), concurrent hub processes sharing one jobs dir, non-Windows kill path (SIGKILL on the child only).
HUMAN: none
PREMISE: right — server.mjs had no job support; every ask_* call awaited the CLI to completion, so a >60 s run was cut off by the bridge.
DONE:
- ask_codex / ask_gemini / ask_copilot / route_task / ask_all take optional wait_s (default 45) and timeout_s; done within wait_s -> same inline answer as before, else {job_id, status:"running", agent, started, next:<poll hint>} and the child keeps running.
- New tools job_status (running/done/failed, elapsed_s, error, last 20 output lines), job_result (full answer; "still running, poll job_status" if not done), jobs (last 24 h, newest first).
- Persistence: .review/hub/jobs/<id>.json (record + final answer) and <id>.out (live child output). A "running" record whose hub is gone (different hub id and its pid not alive, or same pid = in-process restart) is marked failed with "the hub stopped while this job was running…". Records from another live hub process are left alone. Job ids are validated (no path traversal).
- Cap of 4 running jobs (5th call errors with the running ids); per-job timeout 15 min default (AGENT_HUB_JOB_TIMEOUT_MS or per-call timeout_s) with taskkill /T /F /PID; a timed-out job stays failed and does NOT trigger the gemini/copilot -> codex fallback.
- FALLBACK still works inside jobs (tested via route_task with gemini failing).
- Tool descriptions tell Claude to poll job_status and not re-ask.
- fake_agent.mjs: node script that sleeps, prints progress, optional detached grandchild, FOR MEMORY tagged per call. AGENT_HUB_FAKE_CMD switches the fakes to real child processes; existing in-process fakes unchanged.
- Tests: fast path inline, slow path job -> status progress -> result, FOR MEMORY filed once across repeated job_result, restart keeps finished results + fails orphaned job, 4-job cap, timeout tree-kill (grandchild verified dead), route_task/ask_all as jobs + fallback.
NOT DONE:
- split_tasks is unchanged (still blocking) — the task didn't list it; it can still exceed 60 s.
- No pruning of old job files in .review/hub/jobs (jobs() just filters to 24 h).
- Not tried against the live desktop bridge or real CLIs.
ODD:
- FOR MEMORY is filed when the job finishes (by the existing dispatch path), not at job_result time; job_result never re-files. Chose this so a job nobody fetches still lands in the handoff note; "exactly once" holds either way and is tested.
- The grandchild in fake_agent.mjs is spawned detached: a non-detached node grandchild dies with a plain child.kill() anyway (libuv's kill-on-close job object), which would make the kill-tree test pass without taskkill /T. Checked by hand: plain kill leaves the detached grandchild alive, taskkill /T kills it.
- A job counts as failed only if every agent in it failed (relevant for ask_all); partial ask_all failures are "done" with the exit codes in the text.
- A bare (non-job) call's 20-min timer now also uses kill-tree instead of child.kill().
COMMITS: 958850d
