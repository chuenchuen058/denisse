"""
Deterministic checks for the 2026-10-20 "start timer" fixes -- no model, no
real history:

  timers.py      a real timer tool, so there's something to improvise WITH
  brain.py       a "done" with nothing behind it is sent back once
  learning.py    the same shape is labelled bad overnight
  instincts.py   a correction that worked becomes a tool-routing instinct

The model is faked (a scripted ollama.chat), so this checks the wiring, not
whether llama3.1 behaves -- eval/run_eval.py is for that.

    .venv\\Scripts\\python.exe eval\\test_self_correct.py
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402
import instincts  # noqa: E402
import learning  # noqa: E402
import timers  # noqa: E402
import tool_router  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:34} {detail}")


def user(text):
    return {"role": "user", "content": text}


def bot(text):
    return {"role": "assistant", "content": text}


def call(*names):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": n, "arguments": {}}} for n in names]}


def result(text):
    return {"role": "tool", "content": text}


# -- timers -----------------------------------------------------------------

r = timers.set_timer()
check("timer: no length -> asks", r.startswith("No timer started") and "Ask" in r, r)
check("timer: cancel with none running", "no timer running" in timers.cancel_timer().lower())

fired = threading.Event()
timers.set_notifier(lambda t: fired.set())
r = timers.set_timer(seconds=1, label="oven")
check("timer: set", r.startswith("'oven' timer started: 1 second"), r)
check("timer: listed", "'oven'" in timers.list_timers())
check("timer: fires", fired.wait(3), "notifier called within 3s")
check("timer: gone after firing", timers.list_timers() == "No timers running.")

timers.set_timer(minutes=10)
r = timers.cancel_timer()
check("timer: cancel running", r.startswith("Timer stopped with"), r)
check("timer: bad length", timers.set_timer(minutes="abc").startswith("Error"))
timers.set_notifier(None)

# -- routing ----------------------------------------------------------------


def offered(text, msgs=None):
    return {d["function"]["name"] for d in tool_router.select(brain.TOOL_DEFINITIONS, msgs or [user(text)])}


check("router: 'start timer' gets set_timer", "set_timer" in offered("start timer"))
check("router: 'stop timer' gets cancel_timer", "cancel_timer" in offered("stop timer"))
check("router: 'hey' gets no timer", not offered("hey") & {"set_timer", "cancel_timer"})

# -- learning labels the no-tool "Done." -----------------------------------

turns = learning.split_turns([user("stop timer"), bot("Done.")])
v, why = learning.label(turns[0], None)
check("learning: no-tool 'Done.' is bad", v == "bad", why)
turns = learning.split_turns([user("thanks"), bot("Anytime.")])
check("learning: 'Anytime.' is not a claim", learning.label(turns[0], None)[0] == "unknown")

# -- brain: a false "done" is sent back once --------------------------------


class FakeOllama:
    """Replays scripted replies; records what each call was sent."""

    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def chat(self, model, messages, tools=None, **kw):
        self.sent.append({"messages": list(messages), "tools": tools})
        return {"message": self.replies.pop(0)}


def run_turn(replies, text):
    fake = FakeOllama(replies)
    real = brain.ollama
    brain.ollama = fake
    try:
        b = brain.Brain.__new__(brain.Brain)  # skip __init__'s Ollama check
        reply, msgs = b.respond([user(text)])
    finally:
        brain.ollama = real
    return reply, msgs, fake


tool_call = {"content": "", "tool_calls": [{"function": {"name": "cancel_timer", "arguments": {}}}]}
reply, msgs, fake = run_turn([
    {"content": "Done."},                        # the 2026-10-20 reply
    tool_call,                                   # sent back -> uses the tool
    {"content": "There's no timer running."},
], "stop timer")
nudged = any(m.get("role") == "system" and "didn't call any tool" in m.get("content", "")
             for m in fake.sent[1]["messages"])
check("brain: no-tool 'Done.' sent back", nudged and len(fake.sent) == 3, f"{len(fake.sent)} model calls")
check("brain: then reports the real result", reply == "There's no timer running.", reply)
check("brain: nudge never persisted", not any(m.get("role") == "system" for m in msgs))

shell = {"content": "", "tool_calls": [{"function": {"name": "run_shell_command", "arguments": {"command": "rem"}}}]}
reply, msgs, fake = run_turn([
    shell, {"content": "The timer has started."},
    {"content": "That command ran but printed nothing -- I can't confirm a timer started."},
], "start timer")
check("brain: '(no output)' + claim sent back",
      any("no tool this turn confirmed" in m.get("content", "") for m in fake.sent[2]["messages"]), reply)

calc = {"content": "", "tool_calls": [{"function": {"name": "calculate", "arguments": {"expression": "300"}}}]}
reply, msgs, fake = run_turn([
    calc, {"content": "Timer set for 5 minutes."},   # the live run, 2026-10-20
    {"content": "", "tool_calls": [{"function": {"name": "set_timer", "arguments": {"minutes": 5}}}]},
    {"content": "Timer started for 5 minutes."},
], "5 minutes")
timers.cancel_timer()
check("brain: right claim, wrong tool, sent back",
      any(any(phrase in m.get("content", "") for phrase in ("none of them does that", "no tool this turn confirmed")) for m in fake.sent[2]["messages"])
      and len(fake.sent) == 4, reply)
check("router: bare '5 minutes' gets set_timer", "set_timer" in offered("5 minutes"))
check("timer: 'timer' label dropped", timers.set_timer(minutes=1, label="timer").startswith("Timer started"))
timers.cancel_timer()

reply, msgs, fake = run_turn([{"content": "Doing alright, you?"}], "how are you")
check("brain: normal chat untouched", reply == "Doing alright, you?" and len(fake.sent) == 1)

# -- instincts --------------------------------------------------------------

tmp = Path(tempfile.mkdtemp(prefix="denisse-instincts-"))
instincts.INSTINCTS_FILE = str(tmp / "instincts.json")
instincts.INSTINCTS_MD = str(tmp / "instincts.md")
instincts.BRAIN_DIR = str(tmp)

# Promotion now requires independent, explicitly accepted corrections.
os.environ['DENISSE_RULES_FILE'] = str(tmp / 'rules.json')
(tmp / 'rules.json').write_text('{}')

def lofi_trial(number):
    first = user("play lofi"); first["turn_id"] = f"{number}-bad"
    fix = user("no, play lofi on spotify"); fix["turn_id"] = f"{number}-fix"
    feedback = user("that worked"); feedback["turn_id"] = f"{number}-feedback"
    return [first, call("open_application"), result("Error: couldn't find an app called lofi"),
            bot("Couldn't open lofi."), fix, call("play_song"),
            result("Playing Lofi Girl - beats to study to."), bot("Playing some lofi."), feedback]

lofi = lofi_trial(1)
got = instincts.rebuild(lofi)
one = got[0] if got else {}
check("instincts: correction + acceptance", one.get("use") == ["play_song"]
      and set(one.get("when", [])) == {"play", "lofi"}, str(one.get("when")))
check("instincts: files written", Path(instincts.INSTINCTS_FILE).exists() and Path(instincts.INSTINCTS_MD).exists())
check("instincts: one episode not active", not instincts.preferred("play lofi", {"play_song"}))
got = instincts.rebuild(lofi + lofi_trial(2))
check("instincts: two accepted fixes active", "play_song" in instincts.preferred("play lofi", {"play_song"}))
adj = instincts.adjust({"open_application", "calculate"}, "play lofi", protected={"calculate"})
check("instincts: cannot grant new actions", "play_song" not in adj)
check("instincts: preserves existing tools", {"open_application", "calculate"} <= adj)
check("instincts: unrelated message untouched", instincts.adjust({"x"}, "what's the weather") == {"x"})

unrelated = [
    user("turn it off"), call("pause_music"), result("Paused."), bot("Paused."),
    user("i meant the timer"), bot("Sorry."),
    user("what's the news"), call("get_news"), result("Headlines: ..."), bot("Here's the news."),
]
check("instincts: next subject isn't a fix", instincts.derive(learning.label_turns(unrelated)) == [])

data = instincts.load()
data[0]["enabled"] = False
import json  # noqa: E402
Path(instincts.INSTINCTS_FILE).write_text(json.dumps({"instincts": data}), encoding="utf-8")
got = instincts.rebuild(lofi + lofi_trial(2))
check("instincts: his 'off' survives a rebuild", got[0]["enabled"] is False)

print(f"\n{sum(results)}/{len(results)} passed.")
raise SystemExit(0 if all(results) else 1)
