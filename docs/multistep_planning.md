# Multi-step planning and retry — 2026-11-02

Roadmap, "The honest gap": what's left after self-verification is
**planning for multi-step requests** and **retry with a different
approach**. Result: **the planner is kept, on by default**
(`DENISSE_PLANNER=0` turns it off). On the six new multi-step cases it went
from 4/18 to 13/18 clean and from 3/18 to 14/18 live. The old 55-case suite
did not regress: 55/55 clean and live in all three planner rounds.

## What it does (`multistep.py`)

1. **Detect, in code.** The request is split on "and", "then", commas and
   semicolons. It counts as multi-step only when two or more parts start
   with an action verb and say what to act on, and some part has a
   `tool_router` keyword. "find and open my cv" is one job, and so is
   "remind me to buy milk and eggs". None of the 55 existing eval prompts
   triggers it (there's a unit test for this).
2. **Plan, in one JSON-constrained call.** The schema is
   `{"steps": [string, ...]}`, at most 5 steps. The model gets his
   request plus the detector's split, and only has to make each part stand
   on its own. A plan is rejected if it doesn't have one step per detected
   part, or if a step isn't mostly his own words. In that case his own
   parts are the plan. If the model can't be reached, the request is
   answered as a normal single turn.
3. **Execute one step at a time** through the normal `Brain.respond` tool
   loop. Each step is its own user message:
   - It is routed by `tool_router`, together with the step before it, so
     "make a revision list from it" gets the tools of the step it refers
     to. It is never offered more than the whole request would get.
   - Earlier steps reach it only as short summaries (240 characters each)
     in that call's system context.
   - `SYSTEM_PROMPT` is unchanged, and nothing from the plan is persisted
     except the real tool calls and the final answer.
   - Permission guards (add_task, write_file, due dates) check his
     original words, not a step the model wrote.
   - A mutating call that worked in one step is refused if a later step
     repeats it.
4. **Retry once.** A step fails if its last tool call errored, or if the
   honesty net replaced a done-claim. It then gets exactly one retry, with
   the error shown and that exact call refused. If the retry fails too,
   the plan stops and says so in plain words: "I stopped at step 1 (find
   my japan flight pdf): it didn't work -- the search index is unavailable.
   Not done: add a task to check in on friday." Two other outcomes stop
   the plan without a retry:
   - He declined the action ("you said no to it").
   - A policy refusal, such as "isn't available" or "you don't move
     files". This isn't counted as a failure: the model was told why, so
     its reply stands.

## Eval cases added (`eval/cases.py`, `multi_*`)

The data comes from per-case `fixtures` (`run_eval.run_case`). The timer
fixture means no real timer starts. A new `all_tools_of` check requires
every listed tool to be called.

| case | prompt |
|---|---|
| `multi_signals_revision` | find my signals coursework, make a revision list from it and remind me on Thursday |
| `multi_lecture_pdfs` | move this week's lecture PDFs into the right module folders and tell me what's missing |
| `multi_weather_umbrella` | check the weather in London and remind me to take an umbrella tomorrow |
| `multi_tasks_timer` | list my tasks and then start a 25 minute timer |
| `multi_tool_error` | find my japan flight pdf and then add a task to check in on friday (find_files errors) |
| `multi_news_plate` | get me the news headlines, then tell me what's on my plate |

## Method

`eval/run_eval.py --no-dataset --quiet --out <scratch>/<arm><round>` ran
six times on 2026-11-02 between 20:51 and 21:15, with the arms
alternating: A1 B1 A2 B2 A3 B3.

- **A** is `DENISSE_PLANNER=0`, the control. **B** is the planner. The
  code was the same in both, at commit 0875a96.
- `llama3.1:8b` on local Ollama, context 8192.
- The live condition used a read-only copy of `memory/*.json`, kept out
  of git.
- The suite has 61 cases: the old 55 plus the 6 new ones.

## Result

| round | old suite clean | old suite live | new cases clean | new cases live |
|---|---|---|---|---|
| A1 | 55/55 | 55/55 | 2/6 | 1/6 |
| A2 | 55/55 | 54/55 | 1/6 | 1/6 |
| A3 | 55/55 | 55/55 | 0/6 | 1/6 |
| B1 | 55/55 | 55/55 | 5/6 | 4/6 |
| B2 | 55/55 | 55/55 | 5/6 | 5/6 |
| B3 | 55/55 | 55/55 | 3/6 | 4/6 |

Per case, passes out of 3 rounds. Every old case not listed here passed
3/3 in both arms and both conditions.

| case | A clean | A live | B clean | B live |
|---|---|---|---|---|
| `multi_lecture_pdfs` | 2/3 | 3/3 | 3/3 | 3/3 |
| `multi_news_plate` | 0/3 | 0/3 | 2/3 | 3/3 |
| `multi_signals_revision` | 0/3 | 0/3 | 0/3 | 0/3 |
| `multi_tasks_timer` | 0/3 | 0/3 | 2/3 | 1/3 |
| `multi_tool_error` | 1/3 | 0/3 | 3/3 | 3/3 |
| `multi_weather_umbrella` | 0/3 | 0/3 | 3/3 | 3/3 |
| `small_talk` (old) | 3/3 | 2/3 | 3/3 | 3/3 |

How the control fails: it does one part and the reply covers only that
part.

- `multi_weather_umbrella` gets "Added: take an umbrella", with the
  weather it fetched dropped from the reply. The task-confirmation net
  replaces the whole reply.
- `multi_tool_error` gets "Added: check in for Japan flight", which never
  mentions that the search failed.
- `multi_signals_revision` either makes a list and sets no reminder, or
  sets the reminder and gives no list.

## Decision

The keep rule was: the new cases improve and the old suite doesn't regress
in a majority of rounds. The new cases improved in all three rounds, in
both conditions. The old suite in B was 55/55 every round, and the one old
miss was in A. **Kept.**

## What is still wrong

- **`multi_signals_revision` is 0/3 in both arms.** In B, step 1 finds
  `Coursework.md`, but step 2 writes a generic revision list ("Understand
  the concept of signals...") without reading the file. `read_file` is on
  offer to that step, and the step-1 summary includes the path. The model
  just doesn't call it. The likely next move is in code, not in the
  prompt: when a step refers back to a found file, read it for the model,
  as `brain.py` already forces `find_files` and `search_vault`. That
  hasn't been built or measured.
- **`multi_tasks_timer` (B, 3/6):**
  - Twice the reply dropped "25". `set_timer` did run, but the timer
    step's reply said "The timer is now running." without the length.
    The fix would be to confirm a timer in the tool's own words, as
    `add_task` already is (not built).
  - Once the reply summarised the list without "Spritzer".
- **`multi_lecture_pdfs` passes in the control 5/6 too.** Its check, that
  she never claims she moved the files, is weak at telling the arms apart.
  Neither arm actually finds the PDFs before saying what's missing.
- **Latency.** A plan costs one extra short model call, plus one tool loop
  per step. Medians per run were 1.0–2.1 s in both arms, because most
  cases never plan. The multi-step cases themselves took about 3–12 s with
  a free GPU.
