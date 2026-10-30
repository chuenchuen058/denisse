"""Denisse -- a beginner's watchlist: watch, check, explain. Never trade.

Chuen asked on 2026-10-30 for monitoring and research support, not trading:
no orders, no brokerage link, no money moved. One job, done like
study_brief.py -- every step checked, nothing composed by a model:

    watchlist  instruments he added: ticker + exchange + the provider's
               symbol and currency, pinned when added (never guessed later)
    fetch      a quote per instrument from one provider adapter
    validate   source, currency, market date, retrieval time, and a status:
               live / delayed / cached / unavailable. Missing stays missing.
    alert      "above"/"below" thresholds in the instrument's own currency;
               one alert per rule per market date, then a cooldown
    brief      observations (with sources) kept apart from interpretation
    verify     read the saved brief back and check it says what was fetched

Provider: Alpha Vantage (alphavantage.co/documentation, checked 2026-10-30).
GLOBAL_QUOTE has no currency and only a trading date, and on the free tier
"is updated at the end of each trading day", so its quotes are labelled
delayed (end of day), never live. Needs ALPHAVANTAGE_API_KEY in .env; with
no key the provider reports unavailable. The adapter is one class -- swap it.

FixtureProvider is synthetic test data and every quote it makes says so.

Everything lives in memory/invest/ (gitignored): this repo is public.
--fixtures uses memory/invest-fixtures/ instead, never the real watchlist.

    python invest.py search tesco
    python invest.py add TSCO.LON
    python invest.py alert TSCO.LON above 300 GBX
    python invest.py list | remove TSCO.LON
    python invest.py brief            # --fixtures for the synthetic provider
"""
import datetime as dt
import json
import os
import re
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "memory", "invest")
# --fixtures keeps its own store, so a demo never lands on the real watchlist.
FIXTURE_DIR = os.path.join(BASE_DIR, "memory", "invest-fixtures")
STALE_DAYS = 4        # an end-of-day quote older than this (weekend + a holiday) is stale
COOLDOWN_HOURS = 24   # the same alert rule stays quiet this long after firing

FIXTURE_LABEL = "SYNTHETIC FIXTURE -- not market data"


def _path(name):
    return os.path.join(DATA_DIR, name)


def _load(name, default):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(name, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _path(name))


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _num(value):
    """A provider number, or None. Never 0 for a missing value."""
    try:
        if value is None or str(value).strip() in ("", "-", "None", "null"):
            return None
        return float(str(value).strip().rstrip("%"))
    except ValueError:
        return None


# -- providers --------------------------------------------------------------

class ProviderError(Exception):
    pass


class AlphaVantage:
    name = "Alpha Vantage GLOBAL_QUOTE"
    url = "https://www.alphavantage.co/query"
    freshness = "delayed"  # free tier: end of day

    def __init__(self, key=None, get=None):
        self.key = key if key is not None else os.environ.get("ALPHAVANTAGE_API_KEY", "")
        self._get = get

    def _call(self, params):
        if not self.key:
            raise ProviderError("no ALPHAVANTAGE_API_KEY configured")
        get = self._get
        if get is None:
            import requests
            get = lambda u, p: requests.get(u, params=p, timeout=15).json()
        try:
            data = get(self.url, {**params, "apikey": self.key})
        except Exception as e:
            raise ProviderError(f"request failed: {type(e).__name__}")
        if not isinstance(data, dict):
            raise ProviderError("unexpected response")
        for k in ("Note", "Information", "Error Message"):
            if k in data:  # rate limit or bad symbol: say so, don't parse
                raise ProviderError(f"{k}: {str(data[k])[:120]}")
        return data

    def search(self, keywords):
        data = self._call({"function": "SYMBOL_SEARCH", "keywords": keywords})
        return [{"symbol": m.get("1. symbol"), "name": m.get("2. name"), "type": m.get("3. type"),
                 "region": m.get("4. region"), "currency": m.get("8. currency")}
                for m in data.get("bestMatches") or [] if m.get("1. symbol")]

    def quote(self, symbol):
        q = self._call({"function": "GLOBAL_QUOTE", "symbol": symbol}).get("Global Quote") or {}
        if not q or q.get("01. symbol", "").upper() != symbol.upper():
            raise ProviderError(f"no quote returned for {symbol}")
        return {"price": _num(q.get("05. price")), "previous_close": _num(q.get("08. previous close")),
                "change_percent": _num(q.get("10. change percent")),
                "market_date": q.get("07. latest trading day") or None}


class FixtureProvider:
    """Synthetic quotes for tests and demos. Never market data."""
    name = "fixture provider (" + FIXTURE_LABEL + ")"
    freshness = "delayed"

    def __init__(self, quotes=None, matches=None, fail=False):
        self.quotes, self.matches, self.fail = quotes or {}, matches or {}, fail

    def search(self, keywords):
        if self.fail:
            raise ProviderError("fixture provider set to fail")
        key = keywords.upper()
        return list(self.matches.get(key) or self.matches.get(key.split(".")[0], []))

    def quote(self, symbol):
        if self.fail or symbol not in self.quotes:
            raise ProviderError("fixture provider set to fail" if self.fail else f"no quote returned for {symbol}")
        return dict(self.quotes[symbol])


def provider(fixtures=False):
    if fixtures:
        today = dt.date.today().isoformat()
        return FixtureProvider(
            quotes={"DEMO.FIX": {"price": 101.5, "previous_close": 100.0, "change_percent": 1.5,
                                 "market_date": today}},
            matches={"DEMO": [{"symbol": "DEMO.FIX", "name": "Demo Fixture Co (synthetic)", "type": "Equity",
                               "region": "Fixture", "currency": "XXX"}]})
    return AlphaVantage()


# -- watchlist --------------------------------------------------------------

def watchlist():
    return _load("watchlist.json", {"instruments": []})["instruments"]


def add(query, prov=None):
    """Add one instrument. Refuses to choose between several matches."""
    prov = prov or provider()
    query = (query or "").strip()
    if not query:
        return "Tell me the ticker to watch, with its exchange suffix if it has one (e.g. TSCO.LON)."
    try:
        matches = prov.search(query)
    except ProviderError as e:
        return f"I couldn't check {query} with the data provider ({e}), so I haven't added it."
    exact = [m for m in matches if (m["symbol"] or "").upper() == query.upper()]
    if len(exact) != 1:
        if not matches:
            return f"The provider has no instrument matching {query}. Nothing added."
        options = "; ".join(f"{m['symbol']} ({m['name']}, {m['region']}, {m['currency']})" for m in matches[:5])
        return (f"{query} isn't one exact instrument -- it could be: {options}. "
                "Add the exact symbol you mean. Nothing added.")
    m = exact[0]
    if not m.get("currency"):
        return f"The provider didn't say which currency {m['symbol']} trades in, so I haven't added it."
    data = _load("watchlist.json", {"instruments": []})
    if any(i["symbol"] == m["symbol"] for i in data["instruments"]):
        return f"{m['symbol']} is already on your watchlist."
    data["instruments"].append({"symbol": m["symbol"], "name": m["name"], "region": m["region"],
                                "currency": m["currency"], "provider": prov.name,
                                "added": _now().isoformat(timespec="seconds"), "alerts": []})
    _save("watchlist.json", data)
    return f"Added {m['symbol']} ({m['name']}, {m['region']}, priced in {m['currency']}) to your watchlist."


def remove(symbol):
    data = _load("watchlist.json", {"instruments": []})
    keep = [i for i in data["instruments"] if i["symbol"].upper() != (symbol or "").upper()]
    if len(keep) == len(data["instruments"]):
        return f"{symbol} isn't on your watchlist."
    data["instruments"] = keep
    _save("watchlist.json", data)
    return f"Removed {symbol} from your watchlist."


def set_alert(symbol, direction, level, currency):
    """Fires when price >= level ("above") or price <= level ("below"),
    in the instrument's own currency -- a mismatch is refused, not converted."""
    if direction not in ("above", "below"):
        return "An alert is 'above' or 'below' a price."
    lvl = _num(level)
    if lvl is None or lvl <= 0:
        return "The alert needs a positive price."
    data = _load("watchlist.json", {"instruments": []})
    inst = next((i for i in data["instruments"] if i["symbol"].upper() == symbol.upper()), None)
    if inst is None:
        return f"{symbol} isn't on your watchlist; add it first."
    if (currency or "").upper() != inst["currency"].upper():
        return (f"{inst['symbol']} is priced in {inst['currency']}, not {currency}. Give the level in "
                f"{inst['currency']} (GBX is pence: 300 GBX = 3.00 GBP).")
    rule = {"direction": direction, "level": lvl, "currency": inst["currency"]}
    if rule not in inst["alerts"]:
        inst["alerts"].append(rule)
        _save("watchlist.json", data)
    return f"Alert set: {inst['symbol']} at or {direction} {lvl:g} {inst['currency']}."


# -- quotes, alerts, brief ---------------------------------------------------

def fetch(inst, prov, now=None):
    """One validated quote. Status is live/delayed/cached/unavailable."""
    now = now or _now()
    cache = _load("quotes_cache.json", {})
    base = {"symbol": inst["symbol"], "currency": inst["currency"], "source": prov.name,
            "retrieved": now.isoformat(timespec="seconds")}
    try:
        q = prov.quote(inst["symbol"])
    except ProviderError as e:
        old = cache.get(inst["symbol"])
        if old:
            return {**old, "status": "cached", "problem": str(e)}
        return {**base, "status": "unavailable", "problem": str(e), "price": None,
                "previous_close": None, "change_percent": None, "market_date": None}
    quote = {**base, **q, "status": prov.freshness}
    problems = []
    if quote.get("price") is None:
        quote["status"], problems = "unavailable", ["no price in the response"]
    try:
        age = (now.date() - dt.date.fromisoformat(quote["market_date"])).days
    except (TypeError, ValueError):
        age = None
        problems.append("no market date in the response")
        quote["status"] = "unavailable"
    if age is not None and age > STALE_DAYS:
        quote["stale"] = True
        problems.append(f"market date is {age} days old")
    if problems:
        quote["problem"] = "; ".join(problems)
    if quote["status"] != "unavailable":
        cache[inst["symbol"]] = {k: v for k, v in quote.items() if k not in ("status", "problem")}
        _save("quotes_cache.json", cache)
    return quote


def evaluate(inst, quote, now=None):
    """Alerts that fire now. One per rule per market date, then COOLDOWN_HOURS."""
    now = now or _now()
    fired, state = [], _load("alert_state.json", {})
    if quote["status"] in ("unavailable", "cached") or quote.get("stale"):
        return []  # never alert on old or missing data
    for rule in inst.get("alerts", []):
        if rule["currency"] != quote["currency"]:
            continue
        price = quote["price"]
        hit = price >= rule["level"] if rule["direction"] == "above" else price <= rule["level"]
        key = f"{inst['symbol']}|{rule['direction']}|{rule['level']:g}"
        last = state.get(key)
        if not hit:
            continue
        if last and (last["market_date"] == quote["market_date"] or
                     now - dt.datetime.fromisoformat(last["at"]) < dt.timedelta(hours=COOLDOWN_HOURS)):
            continue
        state[key] = {"market_date": quote["market_date"], "at": now.isoformat(timespec="seconds")}
        fired.append({**rule, "price": price})
    if fired:
        _save("alert_state.json", state)
    return fired


def _fmt(value, currency):
    return "unavailable" if value is None else f"{value:,.2f} {currency}"


_LEARN = ("Background, not advice: one company's price can swing a lot day to day (volatility); "
          "spreading money across many holdings (diversification) means no single swing decides "
          "the outcome; fees compound against you over years; and the longer the time horizon, "
          "the more room there is to ride out falls. None of this says what you should buy.")


def brief(prov=None, now=None, out_dir=None):
    """Fetch, validate, evaluate, write a dated brief, read it back."""
    prov, now = prov or provider(), now or _now()
    items = watchlist()
    if not items:
        return "Your watchlist is empty, so there's nothing to brief. Add an instrument first, e.g. 'python invest.py add TSCO.LON'."
    obs, notes, alerts = [], [], []
    for inst in items:
        q = fetch(inst, prov, now)
        fired = evaluate(inst, q, now)
        line = (f"- **{inst['symbol']}** ({inst['name']}): {_fmt(q.get('price'), q['currency'])}; "
                f"previous close {_fmt(q.get('previous_close'), q['currency'])}; "
                f"status **{q['status']}**{' (stale)' if q.get('stale') else ''}; "
                f"market date {q.get('market_date') or 'unavailable'}; retrieved {q['retrieved']}; "
                f"source: {q['source']}")
        if q.get("problem"):
            line += f"; problem: {q['problem']}"
        obs.append(line)
        for a in fired:
            alerts.append(f"- {inst['symbol']} is {_fmt(a['price'], a['currency'])}, at or {a['direction']} "
                          f"your {a['level']:g} {a['currency']} level.")
        cp = q.get("change_percent")
        if q["status"] in ("live", "delayed") and not q.get("stale") and cp is not None:
            size = "a small" if abs(cp) < 1 else "a noticeable" if abs(cp) < 3 else "a large"
            notes.append(f"- {inst['symbol']} moved {cp:+.2f}% on its last trading day -- {size} one-day "
                         "move for a single share. One day says little about the long run.")
        elif q["status"] != "delayed":
            notes.append(f"- {inst['symbol']}: no fresh data, so nothing to interpret.")
    fixture = any(FIXTURE_LABEL in o for o in obs)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    text = "\n".join([
        f"# Watchlist brief -- {stamp}", "",
        *([f"> **{FIXTURE_LABEL}.** Every number below is made up for testing.", ""] if fixture else []),
        "## Observations (from the source named on each line)", "", *obs, "",
        "## Alerts", "", *(alerts or ["- None fired."]), "",
        "## Interpretation (mine, not a fact and not advice)", "", *(notes or ["- Nothing to interpret."]), "",
        "## Background", "", _LEARN, "",
    ])
    out_dir = out_dir or _path("briefs")
    os.makedirs(out_dir, exist_ok=True)
    # Never overwrite an earlier brief: it may hold the only record of an
    # alert that the cooldown now suppresses.
    stem, n = os.path.join(out_dir, f"brief-{now.strftime('%Y%m%d-%H%M%S')}"), 1
    path = stem + ".md"
    while os.path.exists(path):
        n += 1
        path = f"{stem}-{n}.md"
    with open(path, "x", encoding="utf-8") as f:
        f.write(text)
    problems = verify(path, items, obs)
    head = f"Watchlist brief saved to {path}."
    if problems:
        return head + " But the check found: " + "; ".join(problems) + "."
    summary = f" {len(items)} instrument(s), {len(alerts)} alert(s)."
    return head + summary + (" These are synthetic fixtures, not market data." if fixture else "")


def verify(path, items, obs):
    try:
        with open(path, encoding="utf-8") as f:
            saved = f.read()
    except OSError as e:
        return [f"the brief couldn't be read back ({e})"]
    problems = [f"{i['symbol']} is missing" for i in items if f"**{i['symbol']}**" not in saved]
    problems += ["an observation line has no source" for o in obs if "source:" not in o]
    if re.search(r"\b0\.00 [A-Z]{3}\b", saved):
        problems.append("a zero price appears -- check it isn't a missing value")
    if "## Interpretation" not in saved or saved.index("## Observations") > saved.index("## Interpretation"):
        problems.append("observations and interpretation are not separated")
    return problems


def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(BASE_DIR, ".env"))
    except ImportError:
        pass
    global DATA_DIR
    fixtures = "--fixtures" in argv
    if fixtures:
        DATA_DIR = FIXTURE_DIR
    args = [a for a in argv if a != "--fixtures"]
    cmd = args[0] if args else "list"
    prov = provider(fixtures)
    if cmd == "search" and len(args) > 1:
        try:
            for m in prov.search(" ".join(args[1:])):
                print(f"{m['symbol']:12} {m['currency'] or '?':4} {m['region']}: {m['name']}")
        except ProviderError as e:
            print(f"Provider unavailable: {e}")
    elif cmd == "add" and len(args) > 1:
        print(add(args[1], prov))
    elif cmd == "remove" and len(args) > 1:
        print(remove(args[1]))
    elif cmd == "alert" and len(args) == 5:
        print(set_alert(args[1], args[2], args[3], args[4]))
    elif cmd == "brief":
        print(brief(prov))
    elif cmd == "list":
        items = watchlist()
        print("\n".join(f"{i['symbol']} ({i['name']}, {i['currency']}) alerts: "
                        f"{[f'{a['direction']} {a['level']:g}' for a in i['alerts']] or 'none'}"
                        for i in items) or "Watchlist is empty.")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
