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
    # must still go to the model
    ("turn it off", None),
    ("what is the pomodoro technique?", None),
    ("how long is a study mode session", None),
    ("i need to study for computational methods", None),
    ("study mode is annoying, why does it beep", None),
    ("turn off the music", None),
    ("", None),
]

fails = []
for text, want in cases:
    got = match_command(text)
    ok = got == want
    if not ok:
        fails.append(text)
    print(f"[{'OK  ' if ok else 'FAIL'}] {text!r:45} -> {got}" + ("" if ok else f"  (wanted {want})"))

print(f"\n{len(cases) - len(fails)}/{len(cases)} passed.")
sys.exit(1 if fails else 0)
