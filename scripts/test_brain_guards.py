"""Regression test for brain.py's two guards against small-model mistakes.

Both were observed live on 2026-09-23 while testing the task tools:

  * the model called list_tasks(task='the N20 motor') -- a tool taking no
    arguments -- and when the TypeError came back it *invented* a task list
    including an item that has never existed.
  * asked to tick a task off, it replied with a raw JSON status object
    instead of a sentence.

No model calls here; these are pure functions.
"""
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
      brain._run_tool("list_tasks", {"task": "the N20 motor"}, None).startswith(
          ("Nothing on your list", "1 open", "2 open", "3 open", "4 open", "5 open")))

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

print("\n" + ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
