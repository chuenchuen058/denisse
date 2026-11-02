# Orchestra / Atrium discovery baseline — 2 Nov 2026

Section 4 of the campaign brief: what was actually true at the start of the
coordinator session. Observed 2026-11-02 19:52–20:00 UTC (= Europe/London, GMT).
Each line names its source. A generated snapshot (`STATE NOW`) is not used as
evidence for anything below.

## Source checkout

| Fact | Value | Source |
| --- | --- | --- |
| Branch / HEAD | `master` @ `15641c0` (tool-reflex merge, pushed 19:52:48) | `git rev-parse` |
| Dirty | `eval/dataset.jsonl` modified, not staged; owner unknown (not Orchestra) | `git status --short` |
| Ahead of origin | 0 commits | `git log origin/master..master` |
| Remote | `origin` = github.com/chuenchuen058/denisse (public) | `git remote -v` |
| Worktrees | main + 3 in `../denisse-wt/`: agent-exams-router, lane-rate-limits, campaign-controls | `git worktree list` |

The dirty `eval/dataset.jsonl` matters: any task that adds eval cases (the
multistep-planning task does) will hit the runner's dirty-checkout clash
check at merge time until that change is committed or reverted by whoever owns it.

## Scheduler and running processes

| Task | Trigger | Overlap | Logon | Battery | Last result |
| --- | --- | --- | --- | --- | --- |
| Denisse Orchestra | every 2 min | **Parallel** | Interactive | DisallowStart **and StopIfGoingOnBatteries** | `0x8007042B` at 19:52:10 |
| Denisse Eval | daily + logon | IgnoreNew | Interactive | allowed | 0 |
| Denisse Vault Nightly | daily 21:43 | IgnoreNew | Interactive | allowed | 0 |

Source: `Get-ScheduledTask` / `Get-ScheduledTaskInfo`. The live task is
`Parallel`; `orchestra/install_task.ps1` was the IgnoreNew suspect in the
brief. The live setting is what Orchestra's parallel lanes need.

`Interactive` logon means Orchestra runs only while Chuen is logged on. It
is not a 24/7 service while logged off.

**Power incident observed at ~19:53.** `PowerLineStatus: Offline`, battery
100 % (`SystemInformation.PowerStatus`, `Win32_Battery`). Both runner
processes named in the slot locks (PIDs 13304, 25428) were gone, while their
`claude -p` lane children (PIDs 27472, 40132, started 19:48:11 / 19:50:11)
were still running, orphaned. Their `.lane.log` files were 0 bytes. With
`StopIfGoingOnBatteries` Windows ends the tick process only, not its tree: the lanes
survive, nobody collects their report, and `take_lock()` only treats a lock
as stale by age (light/heavy timeout + 900 s ≈ 75 min), never by PID
liveness. Follow-up tasks: crash reconciliation and power-aware dispatch.

## Agents and auth

| CLI | Version |
| --- | --- |
| claude | 2.1.316 (Claude Code) |
| codex | codex-cli 0.162.1 (usage-limited until ~22:55 London per its own message) |
| gemini | 0.66.0 (IneligibleTier earlier today) |
| copilot | GitHub Copilot CLI 1.0.105 |
| ollama | 0.40.13 |
| node / git | v24.19.0 / 2.55.0.windows.3 |

- `claude auth status`: logged in, `authMethod: claude.ai`, `apiProvider: firstParty`
  (subscription, not API key). Identity fields deliberately not copied here.
- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK`: unset in the
  coordinator shell. The repo `.env` holds an API key (present, not read).
  `runner.py` (`lane_env`, ~line 542) strips `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` from every lane.
- `claude --help` offers `--max-budget-usd` (an API-dollar cap, not a Max meter),
  `--output-format stream-json`, `--permission-mode`, `--permission-prompts`.
  No documented non-interactive usage/quota command was found.

## Claude usage observability

| Concept | Observed value | Source |
| --- | --- | --- |
| 5-hour availability | unknown (no rate-limit message seen today) | lane logs, runner.log |
| Weekly availability / reset | **unknown**; Chuen gave Wed 4 Nov 18:00 London as the sprint end | kickoff note |
| Model-specific limit | unknown | — |
| Paid overflow | not verifiable from the CLI; not observed | — |
| Notional spend today | $3.63 of the $400 notional cap | `orchestra/status.py` |

Notional `total_cost_usd` is workload telemetry, not a balance.

## Local inference and hardware

- Ollama: `llama3.1:8b` (4.9 GB) is the only installed model; `ollama ps`
  shows it loaded 100 % GPU, ctx 8192, keep-alive Forever (5.8 GB resident).
- GPU: RTX 5070 Laptop, 8151 MiB, 5724 MiB used, 0 % util (`nvidia-smi`).
  About 2.4 GB of VRAM headroom, so a second model would not fit beside Denisse's.
- CPU: Intel Core Ultra 9 275HX, 24 logical. RAM 31.4 GB, **5.8 GB free**
  with three claude lanes up (`Win32_OperatingSystem`).

## Queue (19:52)

3 running (claude slots 1–3), 15 queued (14 runnable, 1 template draft),
0 parked, 4 PASS / 1 PARTIAL / 3 NO_REPORT today. `orchestra/status.py`,
`.review/runner.log`. The agent-exams-router task got NO_REPORT after 2 s on
the claude lane (attempt 1) with an empty lane log; cause unknown.

## Config and guard

- `orchestra/config.json`: `lane_slots {claude: 3, codex: 1}`, `daily_spend_cap_usd 400`,
  `infra_retries 1`, `parallel_lanes true`, `auto_push true`. The sprint values
  live in config.json until the campaign-controls task moves them.
- Guard: `orchestra/hooks/guard.py` (PreToolUse, `BASH_DENY` table). The push
  privacy check is `orchestra/aftercare.py: privacy_problems` (private paths
  incl. all of `.review/`, key-like strings). The guard-redteam task owns probing both.
- The worker prompt is `orchestra/review_prompt.md`; its version/hash is
  **not** recorded per run (task digest is SHA-1 of the task file only).

## Tests

Not run as part of discovery: the machine was on battery with 5.8 GB RAM
free and three lanes running, and the test-suite-green task owns the
whole-suite baseline. That is a skipped step, not a green result.

## Vault rules conflict (not resolved here)

The vault `AGENTS.md` (updated 2026-10-27) points to `Dev/Memory/` as the
read-first and write-back location and its own structure diagram. `AI-RULES.md`
(2026-10-31, `protection: locked`) defines the Atlas layout, says `Denisse/` is
written by Denisse only, and wins where they disagree. This coordinator follows
the narrower rule: it writes only append lines to `Calendar/Handoff-<date>.md`.
