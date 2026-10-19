"""Regression test for study_mode.match_command -- what typed or spoken
phrases turn study mode on and off. No Tk window, no model.

The first block is what Chuen actually typed on 2026-10-19 that went to
the model instead (found by learning.py). The last block must stay with
the model: questions about studying, and other things that can be "off".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from study_mode import match_command

cases = [
    # real misses, 2026-10-19
    ("study time", "study"),
    ("pomodoro", "study"),
    ("pamodoro", "study"),
    ("off study mode", "stop_study"),
    # the phrases that already worked
    ("study mode", "study"),
    ("Start study mode.", "study"),
    ("study mode on", "study"),
    ("stop study mode", "stop_study"),
    ("end study mode", "stop_study"),
    ("study mode off", "stop_study"),
    ("turn off the study mode please", "stop_study"),
    # natural variants
    ("start a pomodoro", "study"),
    ("pomodoro timer", "study"),
    ("time to study", "study"),
    ("let's study", "study"),
    ("stop the pomodoro", "stop_study"),
    ("pomodoro off", "stop_study"),
    ("stop the study timer", "stop_study"),
    ("I'm done studying", "stop_study"),
    ("i finished studying", "stop_study"),
    # asked for 2026-10-19: ending by naming the session
    ("study session end", "stop_study"),
    ("end study session", "stop_study"),
    ("study session over", "stop_study"),
    ("pomodoro done", "stop_study"),
    ("study timer finished", "stop_study"),
    # 2026-10-19 23:08: he typed this and it went to the model
    ("study sesh", "study"),
    ("start a study sesh", "study"),
    ("study sesh off", "stop_study"),
    # must still go to the model
    ("what is the pomodoro technique?", None),
    ("how long is a study mode session", None),
    ("i need to study for computational methods", None),
    ("study mode is annoying, why does it beep", None),
    ("turn off the music", None),
    ("", None),
]

# Subject-less phrases: the timer only when the timer is running. Asked for
# 2026-10-19; the earlier decision that "turn it off" belongs to the model
# holds for the inactive case, which is why each phrase is asserted BOTH ways.
# Third element is `active`.
context_cases = [
    ("turn it off", True, "stop_study"),
    ("turn it off", False, None),
    ("shut it off", True, "stop_study"),
    ("switch it off", True, "stop_study"),
    ("stop it", True, "stop_study"),
    ("end it", True, "stop_study"),
    ("cancel it", True, "stop_study"),
    ("stop it", False, None),
    ("end it", False, None),
    # bare one-worders -- he typed "off" at 23:08 with the timer on screen
    ("off", True, "stop_study"),
    ("off", False, None),
    ("stop", True, "stop_study"),
    ("stop", False, None),
    ("end", True, "stop_study"),
    ("done", True, "stop_study"),
    ("finished", True, "stop_study"),
    ("enough", True, "stop_study"),
    ("cancel", True, "stop_study"),
    ("done", False, None),
    # still the model's, running or not -- these name a subject that is not
    # the timer, or ask a question
    ("turn off the music", True, None),
    ("turn the music off", True, None),
    ("stop the music", True, None),
    ("is it off", True, None),
    ("turn it on", True, None),
]

fails = []
for text, want in cases:
    # active=False for the unambiguous block: these must match the same way
    # whether or not the timer is running, and False is the harder case.
    got = match_command(text, active=False)
    ok = got == want
    if not ok:
        fails.append(text)
    print(f"[{'OK  ' if ok else 'FAIL'}] {text!r:45} -> {got}" + ("" if ok else f"  (wanted {want})"))

print()
for text, active, want in context_cases:
    got = match_command(text, active=active)
    ok = got == want
    if not ok:
        fails.append(f"{text!r} (active={active})")
    label = f"{text!r} [active={active}]"
    print(f"[{'OK  ' if ok else 'FAIL'}] {label:45} -> {got}" + ("" if ok else f"  (wanted {want})"))

cases = cases + context_cases

print(f"\n{len(cases) - len(fails)}/{len(cases)} passed.")
sys.exit(1 if fails else 0)
