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

# "market brief" answers in chat, from the observations the report was verified against
invest.DATA_DIR = tempfile.mkdtemp(prefix="denisse-invest-chat-brief-")
Q = {"price": 310.0, "previous_close": 300.0, "change_percent": 3.3, "market_date": "2026-10-30"}
rows = {"UP.FIX": Q, "OLD.FIX": dict(Q, market_date="2026-10-20"), "BAD.FIX": dict(Q, price=float("inf"))}
rows.update({f"N{n}.FIX": dict(Q, price=100.0 + n) for n in range(9)})
BP = invest.FixtureProvider(quotes=rows, matches={s: [dict(M, symbol=s, name=f"{s} (synthetic)", region="Fixture",
                                                               currency="XXX", timezone="UTC+00")] for s in rows})
for s in ("UP.FIX", "OLD.FIX", "BAD.FIX"):
    invest.add(s, BP)
invest.set_alert("UP.FIX", "above", "300", "XXX")
r = invest.brief(BP, NOW)
lines = r.splitlines()
obs = {o["symbol"]: o for o in invest.last_observations}
check("brief summary: price, currency, market date, freshness", "- UP.FIX: 310.00 XXX, market date 2026-10-30 "
      "(end of day; retrieved 2026-10-30 18:00 UTC)." in lines, r)
check("brief summary: stale data is flagged", any(l.startswith("- OLD.FIX:") and "stale" in l for l in lines), r)
check("brief summary: unavailable data says so, no number", any(l.startswith("- BAD.FIX: no usable price") and "inf" not in l.lower()
                                                               for l in lines), r)
check("brief summary: the triggered alert", "Alerts: UP.FIX is 310.00 XXX, at or above your 300 XXX level." in lines, r)
check("brief summary: labelled synthetic, and links the full report", invest.FIXTURE_LABEL in lines[0]
      and os.path.isfile(r.split("saved to ")[1].split(".md")[0] + ".md"), r)
check("brief summary: numbers match the verified observations", obs["UP.FIX"]["price"] == 310.0
      and obs["BAD.FIX"]["price"] is None and obs["OLD.FIX"].get("stale"))
check("brief summary: no reasons invented for moves", not any(w in r.lower() for w in ("because", "due to", "driven by")), r)
r = invest.brief(BP, NOW + dt.timedelta(minutes=5))
check("brief summary: nothing fired the second time", "No alerts fired." in r.splitlines(), r)
for s in rows:
    if s.startswith("N"):
        invest.add(s, BP)
r = invest.brief(BP, NOW + dt.timedelta(minutes=10))
check("brief summary: bounded at 8 instruments", sum(l.startswith("- ") for l in r.splitlines()) == 9
      and "- ...and 4 more in the full report." in r and len(r) < 2000, f"{len(r)} chars")
b = brain.Brain.__new__(brain.Brain)
real, brain.ollama = brain.ollama, None
real_prov, invest.provider = invest.provider, lambda fixtures=False: BP
try:
    reply, _ = b.respond([{"role": "user", "content": "market brief"}])
    check("brain: 'market brief' gives the summary, no model call", reply.startswith("Watchlist brief")
          and "Full report saved to" in reply, reply[:200])
finally:
    brain.ollama, invest.provider = real, real_prov

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
