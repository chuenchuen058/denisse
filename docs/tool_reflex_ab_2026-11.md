# Tool-reflex A/B — 2026-11-02

Roadmap section 1, "The tool reflex": would per-tool guidance in the
`recall` and `read_skill` descriptions stop small talk reaching for a tool?
Result: **null. Arm A (current code) is kept.** Arm B broke nothing, but it
had nothing to fix: both target cases were already green in every arm A round.

## Premise, checked first

The roadmap lists two standing failures:

```
clean  small_talk  "do you like rainy days?"  -> called forbidden read_skill
live   greet_hey   "hey"                      -> "I didn't catch that"
```

Neither is still failing:

- `greet_hey` never reaches the model now. `_quick_reply` in `brain.py`
  (commit 53e509e, 2026-10-29) answers greetings and acknowledgements in
  code, so no tool description can affect it. Its eval time is 0.0s.
- `small_talk` failed in the nightlies of 10-21, 10-22, 10-25, 10-26, 10-27
  and 10-29. Every nightly since then (10-30 to 11-02) was all green.
- The suite has 55 cases now, not 27.

## Arms

- **A**: code at 837686c, unchanged.
- **B**: A plus one sentence on each of the two descriptions, nothing else:
  - `recall`: "... by its key. Not for greetings, small talk, opinions, or
    anything you can answer without his saved facts."
  - `read_skill`: "... (see list_skills). Not for greetings, small talk,
    opinions, or anything you can answer without a skill."

## Method

`eval/run_eval.py --no-dataset --out <scratch>` was run 3 times per arm,
A1–A3 then B1–B3, from 19:34 to 19:45 on 2026-11-02. It used
`llama3.1:8b` on local Ollama with a context of 8192. Each run covers clean
(no history) and live (replayed history). For live to match the nightly,
the worktree's `memory/*.json` was a copy of the real memory, used read-only
and kept out of git. The arms ran one after the other, not alternating.

## Result

| round | clean | live |
|---|---|---|
| A1 | 55/55 | 55/55 |
| A2 | 55/55 | 55/55 |
| A3 | 55/55 | 55/55 |
| B1 | 55/55 | 55/55 |
| B2 | 55/55 | 55/55 |
| B3 | 55/55 | 55/55 |

Per case, passes out of 3 rounds:

| case | A clean | A live | B clean | B live |
|---|---|---|---|---|
| `greet_hey` | 3/3 | 3/3 | 3/3 | 3/3 |
| `greet_thanks` | 3/3 | 3/3 | 3/3 | 3/3 |
| `identity` | 3/3 | 3/3 | 3/3 | 3/3 |
| `time` | 3/3 | 3/3 | 3/3 | 3/3 |
| `math` | 3/3 | 3/3 | 3/3 | 3/3 |
| `find_file` | 3/3 | 3/3 | 3/3 | 3/3 |
| `windows_paths` | 3/3 | 3/3 | 3/3 | 3/3 |
| `task_add` | 3/3 | 3/3 | 3/3 | 3/3 |
| `task_list` | 3/3 | 3/3 | 3/3 | 3/3 |
| `timer_start` | 3/3 | 3/3 | 3/3 | 3/3 |
| `timer_set` | 3/3 | 3/3 | 3/3 | 3/3 |
| `timer_stop` | 3/3 | 3/3 | 3/3 | 3/3 |
| `vault_deadline` | 3/3 | 3/3 | 3/3 | 3/3 |
| `vault_not_for_general` | 3/3 | 3/3 | 3/3 | 3/3 |
| `garbled` | 3/3 | 3/3 | 3/3 | 3/3 |
| `greet_morning` | 3/3 | 3/3 | 3/3 | 3/3 |
| `small_talk` | 3/3 | 3/3 | 3/3 | 3/3 |
| `time_elsewhere` | 3/3 | 3/3 | 3/3 | 3/3 |
| `weather` | 3/3 | 3/3 | 3/3 | 3/3 |
| `weather_follow_up` | 3/3 | 3/3 | 3/3 | 3/3 |
| `news` | 3/3 | 3/3 | 3/3 | 3/3 |
| `now_playing` | 3/3 | 3/3 | 3/3 | 3/3 |
| `system_status` | 3/3 | 3/3 | 3/3 | 3/3 |
| `recall_british` | 3/3 | 3/3 | 3/3 | 3/3 |
| `recall_loose` | 3/3 | 3/3 | 3/3 | 3/3 |
| `self_voice` | 3/3 | 3/3 | 3/3 | 3/3 |
| `stop_talking` | 3/3 | 3/3 | 3/3 | 3/3 |
| `shush` | 3/3 | 3/3 | 3/3 | 3/3 |
| `iq_question` | 3/3 | 3/3 | 3/3 | 3/3 |
| `self_parameters` | 3/3 | 3/3 | 3/3 | 3/3 |
| `news_stop` | 3/3 | 3/3 | 3/3 | 3/3 |
| `skills_named` | 3/3 | 3/3 | 3/3 | 3/3 |
| `find_passport` | 3/3 | 3/3 | 3/3 | 3/3 |
| `file_not_app` | 3/3 | 3/3 | 3/3 | 3/3 |
| `latest_cv` | 3/3 | 3/3 | 3/3 | 3/3 |
| `why_not_found` | 3/3 | 3/3 | 3/3 | 3/3 |
| `briefing_real` | 3/3 | 3/3 | 3/3 | 3/3 |
| `ack_no_clock` | 3/3 | 3/3 | 3/3 | 3/3 |
| `complaint_retries` | 3/3 | 3/3 | 3/3 | 3/3 |
| `what_retries` | 3/3 | 3/3 | 3/3 | 3/3 |
| `my_projects` | 3/3 | 3/3 | 3/3 | 3/3 |
| `internship_tracker` | 3/3 | 3/3 | 3/3 | 3/3 |
| `internship_plan` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_plan` | 3/3 | 3/3 | 3/3 | 3/3 |
| `handoff_cover_letter` | 3/3 | 3/3 | 3/3 | 3/3 |
| `handoff_research` | 3/3 | 3/3 | 3/3 | 3/3 |
| `handoff_solutions` | 3/3 | 3/3 | 3/3 | 3/3 |
| `local_reminder_not_handoff` | 3/3 | 3/3 | 3/3 | 3/3 |
| `follow_on_briefing` | 3/3 | 3/3 | 3/3 | 3/3 |
| `follow_on_plan` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_brief_signals` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_brief_em` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_brief_which` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_brief_answer` | 3/3 | 3/3 | 3/3 | 3/3 |
| `task_complete_one` | 3/3 | 3/3 | 3/3 | 3/3 |

How often each tool was called across all 330 replies per arm:

| tool | A | B |
|---|---|---|
| `recall` | 12 | 12 |
| `list_skills` | 6 | 6 |
| `read_skill` | 1 | 0 |

A's one `read_skill` call came in `why_not_found` in A1 (clean). It was
followed by `find_files`, and the case passed. One call in 330 replies is
noise. B made no measurable difference.

## Decision

The keep rule was: B fixes at least one of the two red cases in a majority
of rounds, and breaks nothing that was green in A in a majority of rounds.
B broke nothing. It fixed nothing either, because nothing was red, so the
rule says revert. `brain.py` is unchanged.

The lesson matches the 2026-10-24 note in `tool_router.py`. The
low-content turn is best handled before the model, by a matcher like
`_quick_reply`, and that is what fixed these cases. If `small_talk` turns
red again, arm B is the short change above and can be re-run against it.

## Suggested text for the vault roadmap, section 1

Per the task, the vault was not edited. Text to use there:

> **Closed 2026-11-02 — null result.** Both red cases were already green.
> `greet_hey` is answered in code by `_quick_reply` (2026-10-29), and
> `small_talk` has been green in every nightly since 10-30. The
> description-only A/B (`docs/tool_reflex_ab_2026-11.md`) gave 55/55 clean
> and live in all 3 rounds of each arm. It changed nothing, so it was not
> kept.
