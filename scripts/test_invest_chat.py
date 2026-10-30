"""Watchlist management in chat: invest.chat() and its brain route.

Synthetic fixtures, a temporary store, no model and no network.

    .venv\\Scripts\\python.exe scripts\\test_invest_chat.py
"""
import datetime as dt
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import brain  # noqa: E402
import invest  # noqa: E402

invest.DATA_DIR = tempfile.mkdtemp(prefix="denisse-invest-chat-")
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


NOW = dt.datetime(2026, 10, 30, 18, 0, tzinfo=dt.timezone.utc)
M = {"type": "Equity", "timezone": "UTC+01"}
P = invest.FixtureProvider(matches={
    "ACME": [dict(M, symbol="ACME.LON", name="Acme plc (synthetic)", region="United Kingdom", currency="GBX"),
             dict(M, symbol="ACMEF", name="Acme plc ADR (synthetic)", region="United States", currency="USD")],
    "ACME.LON": [dict(M, symbol="ACME.LON", name="Acme plc (synthetic)", region="United Kingdom", currency="GBX")],
})


def say(text, minutes=0):
    return invest.chat(text, P, NOW + dt.timedelta(minutes=minutes))


def symbols():
    return [i["symbol"] for i in invest.watchlist()]


for t in ("what is a watchlist", "should i buy acme", "what's a good stock", "tell me about acme",
          "add milk to my shopping list", "yes", "1"):
    check(f"not a command: {t!r}", say(t) is None)

r = say("find the ticker for acme")
check("search lists matches with exchange + currency", "ACME.LON" in r and "GBX" in r and "USD" in r, r)

r = say("add acme to my watchlist")
check("ambiguous add asks which, adds nothing", "Which one?" in r and symbols() == [], r)
check("an unrelated 'yes' does not add", say("yes", 1) is None and symbols() == [])
check("...and it used up the pending choice", say("1", 2) is None and symbols() == [])

say("add acme to my watchlist")
r = say("1", 1)
check("the number answers its own pending request", symbols() == ["ACME.LON"], r)
say("add acme to my watchlist")
check("a stale pending choice expires", say("ACMEF", PENDING := invest.PENDING_MINUTES + 1) is None
      and symbols() == ["ACME.LON"])

r = say("alert me if ACME.LON goes above 300")
check("alert without a currency asks, never guesses", "won't guess" in r
      and invest.watchlist()[0]["alerts"] == [], r)
r = say("alert me if ACME.LON goes above 3 GBP")
check("GBP on a GBX instrument refused", "priced in GBX" in r and invest.watchlist()[0]["alerts"] == [], r)
r = say("alert me if ACME.LON goes above 300 GBX")
check("alert set and saved", "Alert set" in r and invest.watchlist()[0]["alerts"][0]["level"] == 300.0, r)
r = say("alert me if ACME.LON goes above nan GBX")
check("non-numeric level does not match a command", r is None or "positive" in r)
check("list alerts", "ACME.LON above 300 GBX" in say("show my alerts"))
r = say("cancel the ACME.LON alert above 300")
check("cancel alert, verified gone", "Cancelled" in r and invest.watchlist()[0]["alerts"] == [], r)
r = say("remove ACME.LON from my watchlist")
check("remove, verified gone", "Removed" in r and symbols() == [], r)

# through the brain: no model is called for these
b = brain.Brain.__new__(brain.Brain)
real, brain.ollama = brain.ollama, None
real_prov = invest.provider
invest.provider = lambda fixtures=False: P
try:
    reply, _ = b.respond([{"role": "user", "content": "add ACME.LON to my watchlist"}])
    check("brain: add via chat, no model call", "Added ACME.LON" in reply and symbols() == ["ACME.LON"], reply)
    reply, _ = b.respond([{"role": "user", "content": "show my watchlist"}])
    check("brain: show watchlist", "ACME.LON" in reply, reply)
finally:
    brain.ollama, invest.provider = real, real_prov

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
