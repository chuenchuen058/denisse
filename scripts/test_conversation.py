"""Regression test for conversation.py.

Guards the bug fixed on 2026-09-04: every face of Denisse replayed the
entire stored history, raw tool traffic included, and the model copied the
garbled wake-word exchanges in it instead of following the system prompt --
the tray popup answered "hey" with a recall() call and "It seems like I
don't have any information stored for 'hey'."

Read-only against the real memory/conversation.json (the one write it
attempts is a mishearing, which is supposed to be refused). Run it after
touching conversation.py, replay(), or the noise gate:

    .venv\\Scripts\\python.exe scripts\\test_conversation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import importlib

fails = []

# 1. Every rewired module still imports.
for m in ["conversation", "brain", "tools", "safety", "memory_store",
          "main", "voice_mode", "wake_word", "tray"]:
    try:
        importlib.import_module(m)
        print(f"[OK ] import {m}")
    except Exception as e:
        print(f"[FAIL] import {m}: {type(e).__name__}: {e}")
        fails.append(m)

import conversation
import memory_store

# 2. The noise gate keeps real commands and drops the mishearings that
#    actually appeared in conversation.json.
NOISE = ["the", "the uh the", "it is to and or", "   "]
REAL = ["hey", "testing", "what time is it", "play heart shaker by twice",
        "find my japan flight pdf", "offline",
        # Garbled but carries content words -- kept on purpose, see
        # looks_like_noise's docstring.
        "the is not playing anything the"]
for t in NOISE:
    ok = conversation.looks_like_noise(t)
    print(f"[{'OK ' if ok else 'FAIL'}] noise      {t!r}")
    if not ok:
        fails.append(f"noise:{t}")
for t in REAL:
    ok = not conversation.looks_like_noise(t)
    print(f"[{'OK ' if ok else 'FAIL'}] kept       {t!r}")
    if not ok:
        fails.append(f"kept:{t}")

# 3. replay() is bounded, prose-only, and never mutates the stored file.
before = memory_store.load_history()
r = conversation.replay()
bad = [m for m in r if m.get("role") == "tool" or m.get("tool_calls")]
print(f"[{'OK ' if not bad else 'FAIL'}] replay prose-only ({len(r)} msgs from {len(before)} stored)")
if bad:
    fails.append("replay-prose")
after = memory_store.load_history()
print(f"[{'OK ' if before == after else 'FAIL'}] replay is read-only")
if before != after:
    fails.append("replay-readonly")

# 3b. Failed turns stay on disk but out of the replay (2026-10-30).
def _t(user, reply, tool=False):
    t = [{"role": "user", "content": user}]
    if tool:
        t += [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "x"}}]},
              {"role": "tool", "content": "r"}]
    return t + [{"role": "assistant", "content": reply}]

hist = (_t("what do you know about my projects?", "BlueBand, the PCB one.")
        + _t("hows my internship tracker", "I don't have any information about your internship tracker.", True)
        + _t("whats the time", "It's 10:57.")
        + _t("thats not what im asking", "It's 10:57.")
        + _t("plan my internship applications", "Here's what I can help with:\n- daily_briefing")
        + _t("what skills do you have?", "Here's what I can help with:\n- daily_briefing")
        + _t("open my cv", "Opened it.") + _t("/bad", "")
        + _t("do you like rainy days?", "I didn't catch that. Could you rephrase?")
        + _t("what's 2+2", "4."))
kept = [g[0]["content"] for g in conversation._drop_failed(conversation._turns(hist))]
want = ["what do you know about my projects?", "what skills do you have?", "/bad", "what's 2+2"]
print(f"[{'OK ' if kept == want else 'FAIL'}] failed turns dropped: {kept}")
if kept != want:
    fails.append("drop-failed")

# 4. record() with a noise transcript writes nothing.
n0 = len(memory_store.load_history())
wrote = conversation.record([{"role": "user", "content": "the uh the"},
                             {"role": "assistant", "content": "eh?"}], source="voice")
n1 = len(memory_store.load_history())
ok = (not wrote) and n0 == n1
print(f"[{'OK ' if ok else 'FAIL'}] voice noise not recorded ({n0} -> {n1})")
if not ok:
    fails.append("record-noise")

print("\n" + ("ALL PASS" if not fails else "FAILED: " + ", ".join(map(str, fails))))
sys.exit(1 if fails else 0)
