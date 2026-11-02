# Did `look_at_this` raise the tool reflex?

2026-11-02. `look_at_this` (desktop_context.py) reads the selection or the
clipboard. It was offered on any message with "this", "that", "these",
"those", "here", "selected", "selection", "highlighted", "clipboard" or
"copied". The question was whether offering it on everyday "that"/"this"
messages made Denisse call it where she shouldn't.

## Method

- Eight new cases in `eval/cases.py`, each with a fake front window and
  selection (or clipboard) that `run_eval._fake_desktop` patches in, so a
  misfire would really read text he never pointed at:
  - **wrong use** (`none_of: look_at_this`): "that's great, thanks!",
    "this is fine, no need to change anything", "ha, do that again" (after
    a joke), "I'm here now, just got home".
  - **right use** (`any_tool_of: look_at_this` + a content check):
    "rewrite this shorter", "translate the selected text into English",
    "summarise what I copied" (terminal in front, so clipboard path), "what
    does that mean?".
- Full suite (65 cases), `eval/run_eval.py --no-dataset`, 3 rounds per arm,
  llama3.1:8b on Ollama on this laptop. Each round runs every case in the
  `clean` and `live` conditions, so each case gets 6 runs per arm.
- **Tool** = the tool part of the check was right (called it / didn't).
  **Full** = the whole case passed (tool, content, leak checks).

## Before: the original cue rule

| case | tool right | full pass |
|---|---|---|
| look_not_thats_great | 6/6 | 6/6 |
| **look_not_this_is_fine** | **0/6** | **0/6** |
| look_not_do_that_again | 6/6 | 6/6 |
| look_not_here | 6/6 | 6/6 |
| look_rewrite_this | 6/6 | 5/6 |
| look_translate_selected | 6/6 | 6/6 |
| look_summarise_copied | 6/6 | 0/6 |
| look_what_does_that_mean | 6/6 | 4/6 |
| explain_this_error (existing) | 6/6 | 6/6 |
| explain_this_text (existing) | 6/6 | 6/6 |

Suite totals per round (clean / live): 61/60, 60/60, 61/61 of 65.

"this is fine, no need to change anything" called `look_at_this` every
time and replied about the lab report in the fake Word window ("you're
already happy with the current state of your lab report draft"). The
other three casual messages had the tool offered too, but the model left
it alone.

## The change

`desktop_context.has_cue()` (used by both the router and the window line in
the system prompt):

- a **strong** cue (selected, selection, highlighted, clipboard, copied)
  still offers the tool on its own;
- a **weak** cue (this, that, these, those, here) now also needs something
  asked of it: an action on text (explain, rewrite, shorten, translate,
  summarise, mean, fix, check, read, look...), a question word (what, how,
  why, is this...), or a `?`. "change" and "do" are left out on purpose.

Across all 65 eval prompts this changes the routing of exactly the four
wrong-use cases and nothing else (checked by comparing old and new rule on
every prompt). The existing cue tests in `scripts/test_desktop_context.py`
still pass; `scripts/test_look_at_this_eval.py` pins the new behaviour.

## After: tightened rule

| case | tool right | full pass |
|---|---|---|
| look_not_thats_great | 6/6 | 6/6 |
| **look_not_this_is_fine** | **6/6** | **6/6** |
| look_not_do_that_again | 6/6 | 6/6 |
| look_not_here | 6/6 | 6/6 |
| look_rewrite_this | 6/6 | 5/6 |
| look_translate_selected | 6/6 | 6/6 |
| look_summarise_copied | 6/6 | 0/6 |
| look_what_does_that_mean | 6/6 | 1/6 |
| explain_this_error (existing) | 6/6 | 6/6 |
| explain_this_text (existing) | 6/6 | 6/6 |

Suite totals per round (clean / live): 60/61, 62/61, 61/61 of 65.

Kept: the misfire went from 6/6 to 0/6, and every right-use case still
calls the tool 6/6.

## Still failing, and not from the cue rule

- **look_summarise_copied 0/6 in both arms.** She calls the tool and gets
  the text, then says only "It looks like you copied some text about
  photosynthesis." -- a topic, not a summary. Answer quality after the
  tool, not reflex.
- **look_what_does_that_mean 4/6 before, 1/6 after.** The tool is offered
  identically under both rules (the prompt has "what" and "?"), and she
  calls it 6/6 both times; the failures are all the same reply: *"It means
  'cheers' is a way of saying thanks or goodbye"*. The last few-shot user
  turn in `brain._STYLE_EXAMPLES` is "cheers", and with no real history the
  model binds a bare "that" to it even after reading the selection. Same
  inputs in both arms, so the 4/6 vs 1/6 gap is sampling noise at n=6 (not
  significant), but the bug is real and pre-dates this change.
- **recall_british, recall_loose 6/6 fail in both arms**: the worktree has
  no `memory/facts.json`, so the recall cases have no facts to recall.
  Environment, unrelated.

Raw per-case logs were in `logs/look_eval/` (git-ignored).
