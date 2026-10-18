"""
Checks for the spoken "should I run that?" gate in wake_word.py -- no
microphone or model needed. The mic loop is simulated by calling
_offer_answer() with transcripts, exactly as run() does.

    .venv\\Scripts\\python.exe eval\\test_voice_confirm.py
"""

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import wake_word as ww  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'OK  ' if ok else 'FAIL'}] {name} {detail}")


# --- the answer classifier --------------------------------------------------
for text, want in [
    ("yes", True), ("yeah go ahead", True), ("sure", True), ("okay do it", True),
    ("no", False), ("nah", False), ("don't", False), ("cancel that", False),
    ("never mind", False),
    # her own question, picked up by the mic -- has both, so not an answer
    ("say yes or no", None),
    ("quick check should i run the command dir say yes or no", None),
    # not an answer at all
    ("", None), ("what's the weather", None),
    ("yes I think that's probably what I meant earlier", None),  # too long
]:
    got = ww._classify_answer(text)
    check(f"classify {text!r}", got == want, f"-> {got}")


def ask(tool, args, gen, timeout=None):
    """Run the confirm callback on its own thread, like _process_command."""
    out = {}
    if timeout is not None:
        ww.CONFIRM_TIMEOUT = timeout
    confirm = ww._make_voice_confirm(None, gen)
    t = threading.Thread(target=lambda: out.setdefault("v", confirm(tool, args)))
    t.start()
    for _ in range(100):  # wait until the question is open
        if ww._pending_confirm is not None or not t.is_alive():
            break
        time.sleep(0.01)
    return t, out


# --- the broker, end to end -------------------------------------------------
gen = ww._start_new_request()

t, out = ask("get_current_time", {}, gen)
t.join(2)
check("ungated tool runs without asking", out.get("v") is True)

t, out = ask("run_shell_command", {"command": "dir Downloads"}, gen)
consumed = ww._offer_answer("yeah go ahead")
t.join(2)
check("spoken yes allows the command", out.get("v") is True and consumed)

t, out = ask("write_file", {"path": r"C:\x\notes.txt", "content": "hi"}, gen)
ww._offer_answer("nope")
t.join(2)
check("spoken no declines the write", out.get("v") is False)

t, out = ask("run_shell_command", {"command": "dir"}, gen)
echoed = ww._offer_answer("say yes or no")  # her own voice, overheard
still_open = ww._pending_confirm is not None
ww._offer_answer("yes")
t.join(2)
check("her own 'say yes or no' is ignored", echoed and still_open and out.get("v") is True)

t, out = ask("run_shell_command", {"command": "dir"}, gen)
passed_on = ww._offer_answer("Denisse what time is it", ["denisse what time is it"])
t.join(2)
check("a new 'Denisse, ...' request declines and is passed on",
      out.get("v") is False and passed_on is False)

t, out = ask("run_shell_command", {"command": "dir"}, gen, timeout=0.3)
t.join(3)
check("silence means no", out.get("v") is False)
ww.CONFIRM_TIMEOUT = 12.0

check("nothing left open", ww._pending_confirm is None)
check("no open question -> speech is not consumed", ww._offer_answer("yes") is False)

ww.VOICE_CONFIRM_ENABLED = False
t, out = ask("run_shell_command", {"command": "dir"}, gen)
t.join(2)
check("DENISSE_VOICE_CONFIRM=0 restores un-gated shell", out.get("v") is True)

print(f"\n{sum(results)}/{len(results)} passed.")
sys.exit(0 if all(results) else 1)
