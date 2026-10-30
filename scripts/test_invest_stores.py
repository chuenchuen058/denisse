"""Reused quotes are revalidated; stores in the wrong shape are refused.

Synthetic fixtures, temporary stores, no model and no network.
Regressions for the 2026-10-30 review of 1ab09aa:
  - a quote reused across midnight became five days old and still alerted
  - {"instruments": null} raised TypeError instead of saying what's wrong

    .venv\\Scripts\\python.exe scripts\\test_invest_stores.py
"""
import datetime as dt
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import brain  # noqa: E402
import invest  # noqa: E402

results = []
UTC = dt.timezone.utc


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


class Counting(invest.FixtureProvider):
    reuse_hours = 6  # like Alpha Vantage's end-of-day reuse

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.asked = 0

    def quote(self, symbol):
        self.asked += 1
        return super().quote(symbol)


def fresh():
    invest.DATA_DIR = tempfile.mkdtemp(prefix="denisse-invest-stores-")
    os.makedirs(invest.DATA_DIR, exist_ok=True)


def write(name, data):
    with open(invest._path(name), "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


def read(name):
    with open(invest._path(name), encoding="utf-8") as f:
        return f.read()


INST = {"symbol": "STALE.FIX", "name": "Stale (synthetic)", "currency": "XXX", "utc_offset": 0.0,
        "alerts": [{"direction": "above", "level": 100.0, "currency": "XXX"}]}


def quotes(date):
    return {"STALE.FIX": {"price": 150.0, "previous_close": 140.0, "change_percent": 7.1, "market_date": date}}


# -- bug 1: reuse is revalidated against the current clock -------------------
fresh()
p = Counting(quotes=quotes("2026-10-26"))
first = invest.fetch(INST, p, dt.datetime(2026, 10, 30, 23, 0, tzinfo=UTC))
check("23:00: a four-day-old quote is still fresh and alerts", not first.get("stale")
      and len(invest.evaluate(INST, first, dt.datetime(2026, 10, 30, 23, 0, tzinfo=UTC))) == 1, first)
later = dt.datetime(2026, 10, 31, 1, 0, tzinfo=UTC)
second = invest.fetch(INST, p, later)
check("01:00, inside the reuse window: now stale, not reused", second.get("stale") and not second.get("reused"), second)
check("...so the provider was asked again", p.asked == 2, p.asked)
check("...and no alert fires on it", invest.evaluate(INST, second, later) == [])

fresh()
p = Counting(quotes=quotes("2026-10-30"))
t0 = dt.datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
invest.fetch(INST, p, t0)
again = invest.fetch(INST, p, t0 + dt.timedelta(hours=2))
check("valid reuse still spares the provider", again.get("reused") and p.asked == 1, again)
check("...keeps the original retrieval time", again["retrieved"] == t0.isoformat(timespec="seconds"), again["retrieved"])
check("...and still alerts", len(invest.evaluate(INST, again, t0 + dt.timedelta(hours=2))) == 1)

good = {"symbol": "STALE.FIX", "currency": "XXX", "source": Counting.name, "source_url": "fixture (no URL)",
        "retrieved": "2026-10-30T18:00:00+00:00", "price": 150.0, "previous_close": 140.0,
        "change_percent": 7.1, "market_date": "2026-10-30"}
legacy = [
    ("not a dict of quotes", ["STALE.FIX"]),
    ("entry not a dict", {"STALE.FIX": "150"}),
    ("no retrieval time", {"STALE.FIX": {k: v for k, v in good.items() if k != "retrieved"}}),
    ("naive retrieval time", {"STALE.FIX": {**good, "retrieved": "2026-10-30T18:00:00"}}),
    ("retrieved in the future", {"STALE.FIX": {**good, "retrieved": "2026-10-31T18:00:00+00:00"}}),
    ("infinite price", {"STALE.FIX": {**good, "price": "Infinity"}}),
    ("NaN price", {"STALE.FIX": {**good, "price": float("nan")}}),
    ("negative price", {"STALE.FIX": {**good, "price": -1}}),
    ("future market date", {"STALE.FIX": {**good, "market_date": "2026-11-30"}}),
    ("bad market date", {"STALE.FIX": {**good, "market_date": "yesterday"}}),
    ("other currency", {"STALE.FIX": {**good, "currency": "USD"}}),
    ("other instrument", {"STALE.FIX": {**good, "symbol": "OTHER.FIX"}}),
    ("other provider", {"STALE.FIX": {**good, "source": "someone else"}}),
]
for name, cache in legacy:
    fresh()
    write("quotes_cache.json", cache)
    p = Counting(quotes={"STALE.FIX": {"price": 120.0, "previous_close": 119.0, "change_percent": 0.8,
                                       "market_date": "2026-10-30"}})
    try:
        q = invest.fetch(INST, p, t0 + dt.timedelta(hours=1))
        ok = p.asked == 1 and not q.get("reused") and q["price"] == 120.0
    except Exception as e:  # noqa: BLE001 -- the regression is a crash
        q, ok = f"{type(e).__name__}: {e}", False
    check(f"cache {name}: not reused, refetched", ok, q)
fresh()
write("quotes_cache.json", {"STALE.FIX": good})
check("a valid saved cache entry is reused as it is", invest.fetch(INST, Counting(), t0 + dt.timedelta(hours=1)).get("reused"))

fresh()
write("quotes_cache.json", {"STALE.FIX": {**good, "market_date": "2026-10-20"}})
q = invest.fetch(INST, Counting(fail=True), t0 + dt.timedelta(hours=1))
check("provider down, old cache: cached + stale, never alerts", q["status"] == "cached" and q.get("stale")
      and invest.evaluate(INST, q, t0) == [], q)
fresh()
write("quotes_cache.json", {"STALE.FIX": {**good, "price": "Infinity"}})
q = invest.fetch(INST, Counting(fail=True), t0 + dt.timedelta(hours=1))
check("provider down, invalid cache: unavailable, no numbers", q["status"] == "unavailable" and q["price"] is None, q)

# -- bug 3: a store in the wrong shape is refused, said and left alone --------
P = invest.FixtureProvider(
    quotes={"ACME.LON": {"price": 310.0, "previous_close": 300.0, "change_percent": 3.3, "market_date": "2026-10-30"}},
    matches={"ACME.LON": [{"symbol": "ACME.LON", "name": "Acme (synthetic)", "region": "UK", "currency": "GBX",
                           "type": "Equity", "timezone": "UTC+00"}]})
ENTRY = {"symbol": "ACME.LON", "name": "Acme (synthetic)", "region": "UK", "currency": "GBX", "utc_offset": 0.0,
         "provider": P.name, "added": "2026-10-30T17:00:00+00:00",
         "alerts": [{"direction": "above", "level": 300.0, "currency": "GBX"}]}
bad_watchlists = [
    {"instruments": None}, [], "just text", {}, {"instruments": {"ACME.LON": {}}}, {"instruments": [None]},
    {"instruments": [{**ENTRY, "name": None}]}, {"instruments": [{k: v for k, v in ENTRY.items() if k != "currency"}]},
    {"instruments": [{**ENTRY, "alerts": None}]}, {"instruments": [{**ENTRY, "alerts": ["above 300"]}]},
    {"instruments": [{**ENTRY, "alerts": [{"direction": "above", "level": "300", "currency": "GBX"}]}]},
    {"instruments": [{**ENTRY, "alerts": [{"direction": "sideways", "level": 300.0, "currency": "GBX"}]}]},
    {"instruments": [{**ENTRY, "utc_offset": "UTC+01"}]},
]
NOW = dt.datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
commands = {
    "list": invest.list_text,
    "add": lambda: invest.add("ACME.LON", P),
    "remove": lambda: invest.remove("ACME.LON"),
    "set alert": lambda: invest.set_alert("ACME.LON", "below", "200", "GBX"),
    "cancel alert": lambda: invest.cancel_alert("ACME.LON", "above", "300"),
    "brief": lambda: invest.brief(P, NOW),
    "chat: show alerts": lambda: invest.chat("show my alerts", P, NOW),
    "chat: add": lambda: invest.chat("add ACME.LON to my watchlist", P, NOW),
}
for bad in bad_watchlists:
    fresh()
    text = json.dumps(bad)
    write("watchlist.json", text)
    failed = []
    for name, run in commands.items():
        try:
            reply = run()
        except Exception as e:  # noqa: BLE001 -- the regression is a crash
            reply = f"RAISED {type(e).__name__}: {e}"
        if "isn't in the shape I expect" not in (reply or "") or "watchlist.json" not in reply:
            failed.append(f"{name}: {reply}")
    check(f"watchlist {text[:50]}: every command explains, none crash", not failed, "; ".join(failed)[:300])
    check(f"watchlist {text[:50]}: file left exactly as it was", read("watchlist.json") == text)
    check(f"watchlist {text[:50]}: no brief written", not os.path.isdir(invest._path("briefs"))
          or os.listdir(invest._path("briefs")) == [])

fresh()
write("watchlist.json", {"instruments": None})
real, invest.provider = invest.provider, lambda fixtures=False: P
try:
    reply, _ = brain.Brain.__new__(brain.Brain).respond([{"role": "user", "content": "show my watchlist"}])
finally:
    invest.provider = real
check("brain: 'show my watchlist' explains the bad file", "isn't in the shape I expect" in reply, reply)

fresh()
write("watchlist.json", {"instruments": [ENTRY]})
check("valid current data loads unchanged", "ACME.LON (Acme (synthetic), GBX) alerts: ['above 300']" in invest.list_text(),
      invest.list_text())
fresh()
legacy_entry = {k: v for k, v in ENTRY.items() if k not in ("utc_offset", "provider")}  # 040f3cb's shape
write("watchlist.json", {"instruments": [legacy_entry]})
before = read("watchlist.json")
check("040f3cb-era entry (no utc_offset) still loads", "ACME.LON" in invest.list_text()
      and read("watchlist.json") == before)

for bad in ([], {"delivered": None}, {"delivered": [1]}, {"ACME.LON|above|300": "sent"},
            {"ACME.LON|above|300": {"at": "not a time"}}):
    fresh()
    write("watchlist.json", {"instruments": [ENTRY]})
    text = json.dumps(bad)
    write("alert_state.json", text)
    try:
        reply = invest.brief(P, NOW)
    except Exception as e:  # noqa: BLE001
        reply = f"RAISED {type(e).__name__}: {e}"
    check(f"alert state {text[:40]}: brief explains, state untouched",
          "alert_state.json isn't in the shape I expect" in reply and read("alert_state.json") == text, reply)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
