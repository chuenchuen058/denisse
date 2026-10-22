"""Regression test for brain.py's two guards against small-model mistakes.

Both were observed live on 2026-09-23 while testing the task tools:

  * the model called list_tasks(task='the N20 motor') -- a tool taking no
    arguments -- and when the TypeError came back it *invented* a task list
    including an item that has never existed.
  * asked to tick a task off, it replied with a raw JSON status object
    instead of a sentence.

No model calls here; these are pure functions.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import brain

fails = []


def check(name, ok):
    print(f"[{'OK ' if ok else 'FAIL'}] {name}")
    if not ok:
        fails.append(name)


print("== dropping unexpected tool arguments ==")


def no_args():
    return "ok"


def one_arg(task: str = ""):
    return f"got {task!r}"


def var_kwargs(**kw):
    return f"got {sorted(kw)}"


check("extra kwarg dropped for a no-arg tool",
      brain._drop_unexpected_args(no_args, {"task": "x"}) == {})
check("known kwarg kept",
      brain._drop_unexpected_args(one_arg, {"task": "x"}) == {"task": "x"})
check("known kept, unknown dropped",
      brain._drop_unexpected_args(one_arg, {"task": "x", "junk": 1}) == {"task": "x"})
check("**kwargs tool untouched",
      brain._drop_unexpected_args(var_kwargs, {"anything": 1}) == {"anything": 1})
check("the real failing call now succeeds",
      re.match(r"Nothing on your list|\d+ open",
               brain._run_tool("list_tasks", {"task": "the N20 motor"}, None)))

print("\n== tool errors must not invite invention ==")
err = brain._run_tool("list_tasks", {}, lambda n, a: True)
check("a working tool returns its own text", "Error" not in err)
bad = brain._run_tool("no_such_tool", {}, None)
check("unknown tool reports an error", bad.startswith("Error: unknown tool"))

print("\n== JSON leak net ==")
cases = [
    ('{"status": "success", "message": "Task completed successfully."}',
     "Done: x", "Task completed successfully."),
    ('{"output": "Added: call the dentist"}', "", "Added: call the dentist"),
    # No human-readable field -- fall back to the real tool result, which is
    # the only thing here that's definitely true.
    ('{"status": "ok"}', "Done: send invoice", "Done: send invoice"),
    ('{"status": "ok"}', "", "Done."),
    # Prose must pass through untouched, including prose mentioning braces.
    ("Sent it, Chuen.", "Done: x", "Sent it, Chuen."),
    ("Your config is {a: 1} roughly", "Done: x", "Your config is {a: 1} roughly"),
    ("", "Done: x", ""),
]
for text, last, want in cases:
    got = brain._unleak_json(text, last)
    check(f"{text[:38]!r} -> {want[:30]!r}", got == want)

print("\n== tool talk net ==")
for text, want in [
    ("I called the get_weather tool to get the current weather in London.", True),
    ("The tool call returned a list of news headlines.", True),
    ("According to the tool, it's raining.", True),
    ("Nothing is playing -- current_track came back empty.", True),
    ("It's 12 degrees and raining in London.", False),
    ("Done, ticked off the Spritzer paperwork.", False),
    ("That's in your toolbox folder.", False),
    ("", False),
]:
    check(f"{text[:40]!r} -> {want}", brain._talks_about_tools(text) == want)

print("\n== role prefix net ==")
for text, want in [
    ("assistant No worries.", "No worries."),
    ("assistant Not much, just hanging around.", "Not much, just hanging around."),
    ("assistant: hi", "hi"),
    ("assistant", "assistant"),            # nothing left -> untouched
    ("Assistant mode is on.", "Assistant mode is on."),
    ("No worries.", "No worries."),
]:
    check(f"role {text!r}", brain._strip_role_prefix(text) == want)

print("\n== narration net ==")
for text, want in [
    ("No tool call needed for that one.", True),
    ("No JSON response is needed for this one.", True),
    ("No JSON is needed for this one.", True),
    ("No worries.", False),
    ("Saved it to settings.json for you.", False),
]:
    check(f"narration {text!r} -> {want}", bool(brain._TOOL_NARRATION.match(text)) == want)

print("\n== news content net ==")
NEWS = ("Today's CNA headlines: IBM falls as analysts question staying power "
        "of software rebound [Business]; Law Society of Singapore elects new "
        "council members after troubled year [Singapore]; Analysis:Anthropic "
        "IPO stands alone as AI market jitters sideline others [Business]; "
        "England's magnificent Brook hits fastest ODI double century [Sport].")
check("empty news reply caught", not brain._has_news_content("Today's news.", NEWS))
check("narrated news reply caught",
      not brain._has_news_content("Here's what's in the news today, Chuen.", NEWS))
check("real summary kept",
      brain._has_news_content("IBM shares fell, and the Law Society has a new council.", NEWS))
check("error result left alone", brain._has_news_content("Couldn't get the news.", "Error: timeout"))
spoken = brain._speak_headlines(NEWS)
check("speaks three headlines, no tags",
      spoken.startswith("Top stories on CNA: IBM falls") and "[" not in spoken
      and "And Anthropic IPO" in spoken and "Brook" not in spoken)

# Real replies from the 2026-10-20 eval report.
for text, want in [
    ("I called the get_weather tool to get the current weather in London. "
     "According to the tool, it's going to be a bit rainy in London today, "
     "with a temperature of +12°C.",
     "It's going to be a bit rainy in London today, with a temperature of +12°C."),
    ("I called the 'current_track' tool to see what's playing, and it said "
     "nothing is currently playing.",
     "Nothing is currently playing."),
    # Nothing but narration: no answer inside, so leave it alone.
    ("The tool call returned a list of news headlines from various categories.",
     "The tool call returned a list of news headlines from various categories."),
    ("It's 12 degrees and raining.", "It's 12 degrees and raining."),
]:
    got = brain._strip_tool_talk(text)
    check(f"strip {text[:34]!r}", got == want)
    if got != want:
        print("      got:", got)

print("\n" + ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
