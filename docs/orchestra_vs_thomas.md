# Orchestra against Thomas's base rules (audit, 12 Nov 2026)

Task `2026-11-12T0104-orchestra-back-to-thomas-s-base-rules-audit-and-re` (Chuen, 12 Nov 2026:
"fix Orchestra following Thomas's base rules and set up improvements").

Orchestra was copied from Thomas van Pul's Atrium core loop on 31 Oct 2026. The base rules, as
this repo writes them down (`.review/README.md`, AGENTS.md Rule 0):

1. **One loop.** A Markdown task in `.review/queue/` → its own worktree and branch → one lane
   does it → the runner re-runs the task's gate → an independent review for risky diffs → merge →
   push via aftercare.
2. **Code goes in by itself; anything that sends as Chuen needs a one-tap approval.**
3. **Not copied, on purpose:** tasks generated from log errors, a model council, anything that
   sends without a one-tap approval.
4. **Rule 0:** softcode, never hardcode.

Evidence is `orchestra/` at master `ebee9c8` and the main checkout's `.review/runner.log` for
11 and 12 Nov 2026 (309 lines, up to 01:05 on the 12th). Log lines are quoted by their time.

## The two days in numbers

| What | Count | Log |
|---|---|---|
| Lane starts | 20 | `START` |
| Don consulted | 20 of 20 starts: 14 only because "every task is consulted", 6 because the task named `tutor/`; 3 answered by Claude | `CONSULT` |
| Lane tabs opened / live windows opened | 20 / 20 | `LANE tab opened`, `LIVE window opened` |
| Merge-only re-runs of an existing branch | 15 | `MERGE-ONLY` |
| Merged and pushed | 5 (plus one PASS with no commits) | `merge review/…: merged` |
| Parked | 21: 14 "reviewer couldn't run", 5 "reviewer blocked it", 1 merge conflict, 1 NO_REPORT twice | `PARKED` |
| Tasks the autopilot wrote | 2, both from one report's NOT DONE list | `AUTOPILOT created` |

Five merges against twenty-one parks: the loop ran, but most of what it produced landed on
hot.md's "Waiting on Chuen" list (32 Orchestra lines at 01:05 on 12 Nov).

## The table

Verdicts: **serves** = serves the loop; **manual** = adds a manual step for Chuen;
**breaks** = breaks a base rule. "Changed" = this task changed it; "recommend" = left alone, see
the section below.

| # | Mechanism | Verdict | Evidence | Action |
|---|---|---|---|---|
| 1 | Queue file → worktree + branch per task | serves (rule 1) | `runner.py:1181` `fresh_worktree`; `KEPT … saved as refs/keep/…` 11 Nov 17:02, 17:54 (commits never lost when a worktree is rebuilt) | none |
| 2 | Runner re-runs the gate before any merge | serves (rule 1) | `runner.py:1823` `run_gate`, `runner.py:2047` `_after_pass`; `runner gate …: pass: 2 command(s) exited 0` 11 Nov 17:11 | none |
| 3 | Combined-tree integration, then fast-forward and push | serves (rule 1) | `runner.py:1277` `integrate`, `aftercare.py:60` `push`; `integration … on 6c022b93: pass` → `pushed 9 commit(s)` 11 Nov 17:16 | none |
| 4 | Independent reviewer for risky diffs | serves (rule 1), and the largest source of manual steps | `runner.py:1345` `risk`, `runner.py:1417` `run_reviewer`; 14 of 21 parks are `reviewer couldn't run (no reviewer lane available …)`, e.g. 12 Nov 00:02, 00:04, 00:06, 00:08, 00:21, 00:23, 00:35, 00:43, 00:50 | owned by "Orchestra: Copilot can review" and "Orchestra: re-queue" (not redone here) |
| 5 | PARTIAL/FAIL park; one retry only for infra failures | serves (Thomas's own number: a re-run FAIL never came back PASS) | `config.json:4` `infra_retries`, `runner.py:2034` | none |
| 6 | Usage limit / expired sign-in is "come back later", not an attempt | serves | `runner.py:623` `detect_limit`; `LIMITED … lane=codex until 2026-11-11 21:58 (attempt not counted)` 11 Nov 18:23 | none |
| 7 | Guard hook, pre-push refusal, watchdog, reconcile after a killed tick | serves (rule 2: a lane can never push or send) | `orchestra/hooks/` (not touched), `runner.py:2435` `reconcile`; `RECONCILE dead owner 3144` 11 Nov 22:46 | none |
| 8 | Daily spend cap with an estimate for runs that never reported | serves, but the estimate shut Claude out for an evening | `runner.py:1955` `charge` prices a dead lane by wall-clock time: `NO_REPORT … after 15574s (attempt 1, ~$98.47 (estimated))` and `after 15346s (… ~$558.46 (estimated))` 11 Nov 22:46, for two lanes that died hours earlier; then `claude daily spend cap $60.00 reached` parked two PASSes at 22:46 and 23:36 | recommend (R1) |
| 9 | **Autopilot writes tasks from reports' NOT DONE / ODD lines and from park reasons** | **breaks** rule 3 (tasks generated from Orchestra's own logs) and adds manual steps | `atrium/autopilot.py:147` `from_reports`, `:160` `from_parked`; `AUTOPILOT created 2026-11-12T0044-follow-up-add-rule-6-and-8-to-task-s-list.md … from not_done 2026-11-09T1135-don-follows-rules-note` 12 Nov 00:44 → `pushed 3 commit(s)` 00:51 that nobody asked for; its twin `…-implement-rules-4-and-12-15-…` → `NO_REPORT` 00:53 and 01:04 → `PARKED … NO_REPORT again after 2 attempts` 01:04, now on hot.md | **changed**: `autopilot_sources_off` |
| 10 | Autopilot writes tasks from the vault Inbox and Chuen's own "Waiting on Chuen" lines | serves (his own words become tasks; nothing is generated from logs) | `atrium/autopilot.py:129` `from_inbox`, `:179` `from_waiting`; anything not shown repo-only is a draft (`config.json:314` `_autopilot`) | kept |
| 11 | **Don consulted on every task** (`consult_applies: all`) | **breaks** rule 3 in effect: a second model's opinion attached to every task and shown to the reviewer. Not a vote, but a standing second voice on work it has no notes for | `consult.py:88` `why`; 20 `CONSULT` lines for 20 starts; a study tutor asked about git merges (`CONSULT …-merge: Don asked` 11 Nov 18:26, 18:30, 23:38, 12 Nov 00:00, 00:22) and again on second attempts (12 Nov 01:04); 3 answers `by claude` (11 Nov 17:55, 23:40, 12 Nov 00:24) spend Claude allowance; this task's own `.review/don.md` says "high confidence" and then describes a different task (lanes, guards, a codex fallback) | **changed**: `consult_narrow_to` |
| 12 | Don consulted on tasks that name the study tooling or say `Consult: don` | serves (5 Nov: "a professional opinion on task"); advisory, can never park or block | `consult.py:77` `_named`, `consult.json` `path_patterns` | kept |
| 13 | **"Orchestra live" window opened beside every lane tab** | **manual**: two windows per run to close | `visible.py:99` `ensure_window`; `LIVE window opened` 20 times, a new pid each time (17760, 43916, 8404, 21556 …), next to 20 `LANE tab opened` | **changed**: `live_window_with_lane_tab` |
| 14 | A Windows Terminal tab per lane run | serves Chuen's 9 Nov ask ("I want to see Claude Code running"); closes itself 30 s after DONE | `visible.py:161` `lane_tab`, `config.json:256` `lane_tabs`; `LANE tab opened … (tab)` | kept; recommend (R4) |
| 15 | Start and finish toasts | serves (nothing to close) | `visible.py:124` `toast` | kept |
| 16 | Tests that open real windows | manual when they ran from a gate; already fixed | only `scripts/test_reply_bubble.py` opens real Tk windows; it is marked `# checks: slow` and `# checks: desktop`, so `run_checks.py` runs it only with `--desktop` (commit `568edb3`, 11 Nov). `orchestra/test_visible.py` replaces `SPAWN`/`RUN` and fails if a real window is reached | none needed |
| 17 | Merge-only re-queue of an existing branch (`requeue.py --merge-only`) | serves (lands a PASS without paying for the lane again) but every one is a command Chuen or the hub chat typed | `runner.py:2216` `merge_only`, `requeue.py:168`; 15 `MERGE-ONLY` runs: 1 merged (11 Nov 17:16), 14 parked again (10 reviewer unavailable, 3 reviewer blocked, 1 merge conflict) | the re-queue is owned by "Orchestra: re-queue" |
| 18 | Merge-only run reviews the branch before it knows the merge can happen | serves, wasteful order | `runner.py:2078` reviewer runs before `runner.py:2126` `integrate`: `review …ollama…: ok by claude after 70s ($1.64)` 11 Nov 18:17:50 → `merge conflict; merge by hand; follow-up … queued` 18:17:52. The 10 Nov branches are stacked on each other (`5196 changed lines`, `3726`, `3286`), so each review reads thousands of lines that are mostly other branches' work | recommend (R2) |
| 19 | Merge follow-up task after a conflict (`<slug>-merge`) | bends rule 3 by the letter (a task written from a merge failure), but it is one task per conflict and it replaces "merge by hand" | `conflicts.py:347` `queue_followup`; `FOLLOW-UP …-merge queued` 11 Nov 18:17; it then cost a 15,574 s dead lane and two more attempts before parking for a reviewer (12 Nov 00:21) | kept; see "Kept on purpose" |
| 20 | Master-health check writes one bug-fix task when master is red | bends rule 3 by the letter (a task written from a failing check); without it every task parks until Chuen fixes master by hand | `master_health.py:360` `_ensure_bugfix`; `…master-is-red-at-ba978dfe…` ran 11 Nov 18:08, `PASS`, `merge …: no commits` 18:14 ($0.44 spent, nothing left for Chuen to do) | kept; see "Kept on purpose" |
| 21 | Conflict-aware dispatch (holds) | serves | `conflicts.py:252` `choose`; `held … (hold 1/2)` then `runs anyway after 2 holds` 11 Nov 18:30 → 12 Nov 00:02 | none |
| 22 | Lane picked by score (`Lane: auto`), fallback when a lane is limited | serves (one lane does it; nobody has to move a task by hand) | `lane_pick.py`; `PICK … -> claude: study: claude score 0.90 … codex limited until 04:40` 12 Nov 00:10 | none |
| 23 | hot.md "Waiting on Chuen" block and the vault day note | serves; every park says why | `runner.py:1944` `park` always writes `<slug>.why.txt` with the reason, `aftercare.py:197` `waiting_lines` puts its first line in hot.md; hot.md at 01:05 on 12 Nov lists 28 parked tasks, each with its reason, and one `PASS not merged: review/2026-11-02T2331-conflict-aware-dispatch — merge conflict; merge by hand` | none needed; recommend (R3) for the wording |
| 24 | Local-team producer and admission (drafts) | manual by design: each admitted task is `Queue: draft` and needs a promotion | `admission.py:474`, `config.json:188` `auto_admit_areas: []`; hot.md lists 2 `Approve draft task …` lines | recommend (R5) |
| 25 | Daily agent exams | serves lane picking; takes no lane slot | `runner.py:2642` `exams_step`; `EXAMS done … (90s)` 11 Nov 16:58 | none |
| 26 | Campaigns, pause / resume, battery policy | serves (Chuen's own controls) | `campaign.py`, `power.py`; `campaign.py pause` 11 Nov 22:50, `resume` 23:34 | not touched (task: don't touch `campaign.json` / `control.json`; battery is owned by "Orchestra: run light work on battery") |
| 27 | Config: every limit in `config.json`, checked against `config_defaults.json` | serves Rule 0 | `runner.py:186` `load_config`, `test_config.py:104` | the three new keys are in both files |

Nothing in `orchestra/` sends as Chuen: lanes cannot push (`--disallowedTools`, the guard hook,
the pre-push hook), and only the runner pushes code after the gate (rule 2).

## What this task changed

All three are new keys; no existing value was edited, and `config.json` and
`config_defaults.json` hold the same values. Each has a `_note` beside it naming this task.

| Key | Value | Effect | To undo |
|---|---|---|---|
| `autopilot_sources_off` | `["not_done", "odd", "parked"]` | The planner no longer writes tasks from the lanes' reports or from park reasons; it still reads the vault Inbox and Chuen's own waiting lines (`atrium/autopilot.py:191` `candidates`) | `[]` |
| `consult_narrow_to` | `"named"` | `consult_applies: all` now means `named`: Don is asked only about a task that says `Consult: don` or names the study tooling (`orchestra/consult.py:88` `why`). By the two days' log that is 6 consults instead of 20 | `""` |
| `live_window_with_lane_tab` | `false` | A lane that gets its own tab no longer also opens the "Orchestra live" window; a lane without a tab still does, and `python orchestra\watch.py` opens it by hand (`orchestra/visible.py:221` `lane_started`) | `true` |

`consult_narrow_to` goes against Chuen's 9 Nov line "every agent goes through Don" as
`consult_applies` implemented it (every *task*). Every agent still goes through Don for study
work, whichever lane runs it. If he meant every task, `""` restores it.

Tests: `orchestra/test_consult.py` (`test_narrow_to_caps_the_rule_without_editing_it`,
`test_shipped_rule_is_narrowed_to_named_tasks`), `orchestra/test_autopilot.py`
(`test_report_follow_ups_and_bug_fixes_from_partial_reports`, extended), `orchestra/test_visible.py`
(`test_a_lane_with_its_own_tab_does_not_also_open_the_live_window`). Four existing tests (two more in `orchestra/test_runner.py`) assumed
the old shipped behaviour and now set the key they are about (`consult_narrow_to=""`,
`autopilot_sources_off=[]`); what they assert is unchanged.

## Kept on purpose

- **Merge follow-up tasks (row 19) and the master-is-red bug-fix task (row 20).** By the letter
  both are tasks written from a failure. They are kept because each is one task for one named
  event, with a fixed template, and switching either off puts the work back on Chuen ("merge by
  hand", or every task parking while master is red). The autopilot's report and parked sources
  were different in kind: open-ended, a model deciding what a log line means, and three days
  late. `conflict_followup: false` and `master_health: false` are the existing switches if he
  wants the letter of the rule.
- **Lane tabs and toasts (rows 14, 15).** His own 9 Nov request, and the tab closes itself.

## Recommended, not done

- **R1. Cap what a dead lane can be charged.** `charge` (`runner.py:1955`) prices a run that
  never reported by wall-clock seconds, so a lane that died while the laptop slept was charged
  ~$558 and Claude was over the cap for the rest of 11 Nov, both as a task lane and as a
  reviewer. A lane cannot have run longer than its timeout: charge at most that. Not done here
  because two parked branches rewrite the same function (`2026-11-09T1050-honest-cost-logging`,
  `2026-11-10T1246-credits-used-per-task-per-agent`); a third edit would conflict with both.
- **R2. On a merge-only run, test the merge before paying for the review.** A
  `git merge-tree` check in `_after_pass` before the reviewer would have saved the $1.64 review
  at 11 Nov 18:17 that was followed two seconds later by "merge conflict". It costs money, not
  Chuen's time, and `_after_pass` is where "Orchestra: Copilot can review" and "Orchestra:
  re-queue" are working now, so it is left for after they land.
- **R3. The reviewer-unavailable park reason says "after signing in"** (`runner.py:2090`) when
  the cause was a usage limit or the spend cap. hot.md does give the true cause in brackets, so
  nothing is hidden; the wording belongs to "Orchestra: re-queue", which replaces that park.
- **R4. Lane tabs may take the focus.** `wt.exe -w orchestra new-tab` (`config.json`
  `lane_tab_argv`) can bring the Terminal window forward; the live window was opened with
  "show, don't activate" but a tab can't be. Not measured here (it needs a desktop session), so
  not changed. If it interrupts typing, `lane_tab_lanes` is the list to shorten.
- **R5. Drafts.** Local-team tasks land as `Queue: draft` (`auto_admit_areas: []`) and each one
  is a line for Chuen to approve. That is the one-tap rule applied to work a local model wrote,
  which is stricter than rule 2 asks for code. Leave it until the producer's drafts have a
  track record; then add areas to `auto_admit_areas`.

## The three biggest manual steps left for Chuen

1. **Re-queuing PASSes that parked because no reviewer could run.** 14 of the 21 parks in two
   days, each needing `requeue.py <slug> --merge-only` once a reviewer is back. Claude writes
   most changes, so the only independent reviewer is Codex, which was limited from 18:23 to
   21:58 on 11 Nov and again from 23:51 until 04:40 on 12 Nov. Owned by "Orchestra: Copilot can review" and "Orchestra: re-queue".
2. **Reviewer blocks.** 5 parks. Each needs someone to read `<slug>.review.md`, decide what to
   keep, and re-queue with a note (the `REQUEUED … Reviewer block …` lines on 11 Nov 17:53,
   18:07 and 12 Nov 00:49 were typed by Chuen or the hub chat). Nothing feeds a block back to
   the lane that wrote the change.
3. **The stacked 10 Nov branches.** At least six parked branches carry each other's commits
   (2,000 to 5,000 changed lines apiece in the `review …: touches …` lines) and all edit
   `runner.py` and `config.json`. They can
   only land one at a time, each landing changes the next one's diff, and every attempt needs
   a fresh review. Until they are landed in order, or dropped, they keep producing parks
   whatever else is fixed. This one is a decision about which branches still matter, not a
   mechanism.
