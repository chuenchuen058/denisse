"""invest.py -- watchlist -> quote -> validate -> alert -> brief -> verify.

Synthetic fixtures and a temporary store only: no network, no API key, and
nothing here is market data. The Alpha Vantage shapes are the documented
demo responses (GLOBAL_QUOTE for IBM, SYMBOL_SEARCH for "tesco"), with
prices replaced by made-up numbers.

    .venv\\Scripts\\python.exe scripts\\test_invest.py
"""
import datetime as dt
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import invest  # noqa: E402

TMP = tempfile.mkdtemp(prefix="denisse-invest-")
invest.DATA_DIR = TMP
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


NOW = dt.datetime(2026, 10, 30, 18, 0, tzinfo=dt.timezone.utc)
TODAY = NOW.date().isoformat()
MATCHES = {
    "TSCO": [{"symbol": "TSCO.LON", "name": "Tesco PLC", "type": "Equity", "region": "United Kingdom", "currency": "GBX"},
             {"symbol": "TSCDF", "name": "Tesco plc", "type": "Equity", "region": "United States", "currency": "USD"}],
    "TSCO.LON": [{"symbol": "TSCO.LON", "name": "Tesco PLC", "type": "Equity", "region": "United Kingdom", "currency": "GBX"}],
    "NOCUR.FIX": [{"symbol": "NOCUR.FIX", "name": "No currency", "type": "Equity", "region": "X", "currency": None}],
}


def prov(price=310.0, date=TODAY, fail=False, **extra):
    q = {"price": price, "previous_close": 300.0, "change_percent": 3.33, "market_date": date, **extra}
    return invest.FixtureProvider(quotes={"TSCO.LON": q}, matches=MATCHES, fail=fail)


# -- watchlist ---------------------------------------------------------------
check("starts empty", invest.watchlist() == [])
r = invest.add("TSCO", prov())
check("ambiguous ticker refused, options listed", "isn't one exact instrument" in r and "TSCDF" in r
      and invest.watchlist() == [], r)
r = invest.add("NOCUR.FIX", prov())
check("no currency -> not added", "didn't say which currency" in r and invest.watchlist() == [], r)
r = invest.add("TSCO.LON", prov(fail=True))
check("provider down -> not added", "couldn't check" in r and invest.watchlist() == [], r)
r = invest.add("TSCO.LON", prov())
w = invest.watchlist()
check("exact symbol added with currency + provider", len(w) == 1 and w[0]["currency"] == "GBX"
      and "fixture" in w[0]["provider"], r)
check("duplicate add refused", "already" in invest.add("TSCO.LON", prov()) and len(invest.watchlist()) == 1)
check("persisted privately in the store dir", os.path.exists(os.path.join(TMP, "watchlist.json")))

# -- alerts ------------------------------------------------------------------
r = invest.set_alert("TSCO.LON", "above", "3", "GBP")
check("currency mismatch refused (GBP vs GBX)", "priced in GBX" in r and not invest.watchlist()[0]["alerts"], r)
check("unknown direction refused", "above' or 'below" in invest.set_alert("TSCO.LON", "near", "300", "GBX"))
check("non-numeric level refused", "positive" in invest.set_alert("TSCO.LON", "above", "abc", "GBX"))
r = invest.set_alert("TSCO.LON", "above", "300", "GBX")
check("alert set", invest.watchlist()[0]["alerts"] == [{"direction": "above", "level": 300.0, "currency": "GBX"}], r)

inst = invest.watchlist()[0]
q = invest.fetch(inst, prov(), NOW)
check("quote: source, currency, market date, retrieval time, status",
      q["source"].startswith("fixture") and q["currency"] == "GBX" and q["market_date"] == TODAY
      and q["retrieved"].startswith("2026-10-30T18:00") and q["status"] == "delayed", str(q))
fired = invest.evaluate(inst, q, NOW)
check("alert fires at >= level", len(fired) == 1)
check("evaluating alone records nothing (delivery does)", len(invest.evaluate(inst, q, NOW)) == 1)
invest._acknowledge(fired, NOW)  # as brief() does once the report is saved and verified
check("same market date: no duplicate", invest.evaluate(inst, q, NOW + dt.timedelta(hours=30)) == [])
q2 = invest.fetch(inst, prov(date="2026-10-31"), NOW + dt.timedelta(hours=2))
check("new date inside cooldown: quiet", invest.evaluate(inst, q2, NOW + dt.timedelta(hours=2)) == [])
check("new date after cooldown: fires again", len(invest.evaluate(inst, q2, NOW + dt.timedelta(hours=25))) == 1)
q3 = invest.fetch(inst, prov(price=299.99), NOW)
check("below the level: no 'above' alert", invest.evaluate(inst, q3, NOW + dt.timedelta(days=5)) == [])

# -- freshness / availability -------------------------------------------------
stale = invest.fetch(inst, prov(date="2026-10-20"), NOW)
check("10-day-old quote is stale", stale.get("stale") and "10 days old" in stale["problem"], str(stale))
check("stale quote never alerts", invest.evaluate(inst, stale, NOW + dt.timedelta(days=9)) == [])
missing = invest.fetch(inst, prov(price=None), NOW)
check("missing price stays None, status unavailable", missing["price"] is None
      and missing["status"] == "unavailable", str(missing))
check("_num never turns blank into 0", invest._num("") is None and invest._num(None) is None
      and invest._num("1.9983%") == 1.9983)
cached = invest.fetch(inst, prov(fail=True), NOW)
check("provider down after a good quote -> cached, labelled", cached["status"] == "cached"
      and "fail" in cached["problem"], str(cached))
check("cached quote never alerts", invest.evaluate(inst, cached, NOW + dt.timedelta(days=9)) == [])
os.remove(os.path.join(TMP, "quotes_cache.json"))
down = invest.fetch(inst, prov(fail=True), NOW)
check("provider down, no cache -> unavailable, no number", down["status"] == "unavailable" and down["price"] is None)

# -- Alpha Vantage adapter (documented response shapes, fake transport) -------
DEMO = {"Global Quote": {"01. symbol": "TSCO.LON", "02. open": "1", "05. price": "305.5000",
                         "07. latest trading day": TODAY, "08. previous close": "300.0000",
                         "10. change percent": "1.8333%"}}
av = invest.AlphaVantage(key="test", get=lambda u, p: DEMO)
check("adapter parses GLOBAL_QUOTE", av.quote("TSCO.LON") == {"price": 305.5, "previous_close": 300.0,
                                                              "change_percent": 1.8333, "market_date": TODAY})
try:
    invest.AlphaVantage(key="").quote("TSCO.LON")
    check("no key -> ProviderError", False)
except invest.ProviderError as e:
    check("no key -> ProviderError", "ALPHAVANTAGE_API_KEY" in str(e))
for body, why in (({"Note": "rate limit"}, "rate limit"), ({"Global Quote": {}}, "empty"),
                  ({"Global Quote": {"01. symbol": "OTHER"}}, "wrong symbol")):
    try:
        invest.AlphaVantage(key="t", get=lambda u, p, b=body: b).quote("TSCO.LON")
        check(f"adapter refuses {why}", False)
    except invest.ProviderError:
        check(f"adapter refuses {why}", True)
s = invest.AlphaVantage(key="t", get=lambda u, p: {"bestMatches": [{"1. symbol": "TSCO.LON", "2. name": "Tesco PLC",
                                                                     "4. region": "United Kingdom", "8. currency": "GBX"}]})
check("adapter parses SYMBOL_SEARCH currency", s.search("tesco")[0]["currency"] == "GBX")

# -- brief ---------------------------------------------------------------------
if os.path.exists(os.path.join(TMP, "alert_state.json")):
    os.remove(os.path.join(TMP, "alert_state.json"))
out = os.path.join(TMP, "briefs")
r = invest.brief(prov(), NOW, out)
path = r.split("saved to ")[1].split(".md")[0] + ".md"
text = open(path, encoding="utf-8").read()
check("brief saved and verified", "check found" not in r and "1 alert(s)" in r, r)
check("brief labels fixtures as synthetic", invest.FIXTURE_LABEL in text and "synthetic" in r)
check("observations cite source, before interpretation",
      "source: fixture" in text and text.index("## Observations") < text.index("## Interpretation"))
check("interpretation is plain-language, not advice", "+3.33%" in text and "not advice" in text
      and "should buy" not in text.replace("says what you should buy", ""))
r = invest.brief(prov(fail=True), NOW + dt.timedelta(days=1), out)
text = open(r.split("saved to ")[1].split(".md")[0] + ".md", encoding="utf-8").read()
check("brief with provider down: cached, no alert, nothing interpreted",
      "status **cached**" in text and "None fired" in text and "nothing to interpret" in text, r)
r2 = invest.brief(prov(fail=True), NOW + dt.timedelta(days=1), out)
check("a second brief in the same second doesn't overwrite the first",
      r2.split("saved to ")[1].split(".md")[0] != r.split("saved to ")[1].split(".md")[0], r2)
check("verify catches a missing instrument",
      invest.verify(path, [{**invest.last_observations[0], "symbol": "ZZZ"}]) != [])

check("remove", "Removed" in invest.remove("TSCO.LON") and invest.watchlist() == [])
check("empty watchlist brief says so", "empty" in invest.brief(prov(), NOW, out))

# -- chat route: read-only, no model call -------------------------------------
import brain  # noqa: E402

b = brain.Brain.__new__(brain.Brain)  # no Ollama: these replies never reach the model
real_ollama, brain.ollama = brain.ollama, None
try:
    reply, _ = b.respond([{"role": "user", "content": "show my watchlist"}])
    check("chat: empty watchlist said plainly", reply == "Your watchlist is empty.", reply)
    reply, _ = b.respond([{"role": "user", "content": "market brief"}])
    check("chat: brief on empty watchlist", "empty" in reply, reply)
finally:
    brain.ollama = real_ollama
for t in ("add tesco to my watchlist", "should i buy tesco", "what is a watchlist", "brief me on signals"):
    check(f"chat: {t!r} not routed", not (brain._WATCHLIST_SHOW.fullmatch(t) or brain._WATCHLIST_BRIEF.fullmatch(t)))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
