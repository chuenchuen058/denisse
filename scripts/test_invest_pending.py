"""A pending watchlist choice answers only the question just asked.

Through Brain.respond(), with a scripted fake model (an unscripted model
call or any tool call fails the test), a synthetic provider and temporary
conversation, pending-choice and watchlist stores.

Regression for the 2026-10-30 review: "add acme to my watchlist" -> which
one? -> "thanks" ("Any time.", an early reply that never reached
invest.chat) -> "1" added ACME.LON.

    .venv\\Scripts\\python.exe scripts\\test_invest_pending.py
"""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="denisse-pending-"))
os.environ["DENISSE_RULES_FILE"] = str(TMP / "rules.json")
(TMP / "rules.json").write_text("{}", encoding="utf-8")
os.environ.setdefault("DENISSE_VAULT", str(TMP / "vault"))

import brain  # noqa: E402
import invest  # noqa: E402
import memory_store  # noqa: E402

memory_store.MEMORY_DIR = str(TMP)
memory_store.MEMORY_FILE = str(TMP / "conversation.json")
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


M = {"type": "Equity", "timezone": "UTC+01"}
P = invest.FixtureProvider(matches={
    "ACME": [dict(M, symbol="ACME.LON", name="Acme plc (synthetic)", region="United Kingdom", currency="GBX"),
             dict(M, symbol="ACMEF", name="Acme plc ADR (synthetic)", region="United States", currency="USD")],
    "BETA": [dict(M, symbol="BETA.LON", name="Beta plc (synthetic)", region="United Kingdom", currency="GBX"),
             dict(M, symbol="BETAX", name="Beta ADR (synthetic)", region="United States", currency="USD")],
})
for key, alts in (("ACME", ("ACME.LON", "ACMEF")), ("BETA", ("BETA.LON", "BETAX"))):
    for n, sym in enumerate(alts):  # add() re-checks the chosen symbol with the provider
        P.matches[sym] = [P.matches[key][n]]
CLOCK = [dt.datetime(2026, 10, 30, 18, 0, tzinfo=dt.timezone.utc)]
MODEL_REPLY = "Sorry, what does that number refer to?"


class FakeOllama:
    def __init__(self):
        self.calls = 0

    def chat(self, model, messages, tools=None, **kw):
        self.calls += 1
        return {"message": {"content": MODEL_REPLY}}


def no_tools(name, args):
    raise AssertionError(f"unexpected tool call {name}({args})")


def conversation(*texts, minutes_between=0):
    """Run the texts as one conversation; returns (replies, model calls)."""
    fake = FakeOllama()
    real = brain.ollama, brain._run_tool, invest.provider, invest._now
    brain.ollama, brain._run_tool = fake, no_tools
    invest.provider = lambda fixtures=False: P
    invest._now = lambda: CLOCK[0]
    replies, messages = [], []
    try:
        for t in texts:
            messages.append({"role": "user", "content": t})
            b = brain.Brain.__new__(brain.Brain)
            reply, messages = b.respond([dict(m) for m in messages])
            replies.append(reply)
            CLOCK[0] += dt.timedelta(minutes=minutes_between)
    finally:
        brain.ollama, brain._run_tool, invest.provider, invest._now = real
    return replies, fake.calls


def fresh():
    invest.DATA_DIR = tempfile.mkdtemp(prefix="store-", dir=TMP)


def symbols():
    return [i["symbol"] for i in invest.watchlist()]


def pending():
    try:
        with open(invest._path("pending.json"), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


fresh()
r, calls = conversation("add acme to my watchlist", "1")
check("asks which one, adds nothing yet", "Which one?" in r[0] and "1 = ACME.LON" in r[0], r[0])
check("immediate number picks from that question", r[1].startswith("Added ACME.LON") and symbols() == ["ACME.LON"]
      and calls == 0, f"{r[1]} | model calls {calls}")
check("...and the choice is used up", pending() == {}, pending())

fresh()
r, calls = conversation("add acme to my watchlist", "ACMEF")
check("immediate exact symbol picks from that question", r[1].startswith("Added ACMEF") and symbols() == ["ACMEF"]
      and calls == 0, r[1])

fresh()
r, calls = conversation("add acme to my watchlist", "thanks", "1")
check("review repro: 'thanks' still gets its quick reply", r[1] == "Any time.", r[1])
check("review repro: a later '1' adds nothing", symbols() == [] and r[2] == MODEL_REPLY and calls == 1,
      f"{r[2]} | {symbols()} | model calls {calls}")
check("review repro: the stale choice is gone", pending() == {}, pending())

fresh()
r, calls = conversation("add acme to my watchlist", "hey", "ACMEF")
check("greeting still answered without the model", r[1].startswith("Hey") and calls == 1, r[1])
check("an old symbol after a greeting adds nothing", symbols() == [], f"{r[2]} | {symbols()}")

fresh()
r, calls = conversation("add acme to my watchlist", "tell me a joke", "1")
check("an unrelated request then an old number adds nothing", symbols() == [] and calls == 2,
      f"{r} | model calls {calls}")

fresh()
r, calls = conversation("add acme to my watchlist", "1", minutes_between=invest.PENDING_MINUTES + 1)
check("an expired choice stays expired", symbols() == [] and r[1] == MODEL_REPLY, r[1])

fresh()
r, calls = conversation("add acme to my watchlist", "add beta to my watchlist", "ACMEF")
check("a new question replaces the old one", "1 = BETA.LON" in r[1] and symbols() == [], f"{r[2]} | {symbols()}")
fresh()
r, calls = conversation("add acme to my watchlist", "add beta to my watchlist", "2")
check("...and its numbers are the new options", symbols() == ["BETAX"], f"{r[2]} | {symbols()}")

fresh()
r, calls = conversation("add acme to my watchlist", "1", "2")
check("a choice answers once: the next number goes to the model", symbols() == ["ACME.LON"]
      and r[2] == MODEL_REPLY, f"{r[2]} | {symbols()}")

fresh()
question = invest.chat("add acme to my watchlist", P, CLOCK[0])
answers, barrier = [], threading.Barrier(8)


def answer():
    barrier.wait()
    answers.append(invest.chat("1", P, CLOCK[0], last_said=question))


threads = [threading.Thread(target=answer) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("eight simultaneous answers: exactly one is taken", sum(a is not None for a in answers) == 1
      and symbols() == ["ACME.LON"], answers)

fresh()
os.makedirs(invest.DATA_DIR, exist_ok=True)
for bad in ({"options": None, "at": "x"}, ["ACME.LON"], {"question": "q", "options": ["ACME.LON"], "at": "2026-10-30T18:00:00"}):
    with open(invest._path("pending.json"), "w", encoding="utf-8") as f:
        json.dump(bad, f)
    r, calls = conversation("1")
    check(f"invalid pending choice {json.dumps(bad)[:40]}: no crash, nothing added",
          symbols() == [] and r[0] == MODEL_REPLY, r[0])

fresh()
r, calls = conversation("hey", "thanks", "ok")
check("greetings and acknowledgements unchanged, no model",
      r == ["Hey Chuen.", "Any time.", "Good."] and calls == 0, f"{r} | {calls}")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
