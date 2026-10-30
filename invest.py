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
import math
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "memory", "invest")
# --fixtures keeps its own store, so a demo never lands on the real watchlist.
FIXTURE_DIR = os.path.join(BASE_DIR, "memory", "invest-fixtures")
STALE_DAYS = 4        # an end-of-day quote older than this (weekend + a holiday) is stale
COOLDOWN_HOURS = 24   # the same alert rule stays quiet this long after a delivered alert
LATEST_UTC_OFFSET = 14  # UTC+14 is the furthest-ahead clock on Earth

FIXTURE_LABEL = "SYNTHETIC FIXTURE -- not market data"


class StoreError(Exception):
    """A private file exists but can't be read. Never treated as empty:
    an empty watchlist saved over it would erase what was there."""


def _path(name):
    return os.path.join(DATA_DIR, name)


def _load(name, default):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        raise StoreError(f"{name} is unreadable ({type(e).__name__}); I haven't changed it")


def _save(name, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _path(name))


_thread_lock = threading.RLock()


@contextmanager
def _locked():
    """One read-modify-write at a time, across the CLI and the tray."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with _thread_lock, open(_path(".lock"), "a+b") as lock:
        for _ in range(200):
            try:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(.025)
        else:
            raise StoreError("the watchlist is busy in another window; try again")
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _num(value):
    """A finite provider number, or None. Never 0 for a missing value, and
    never NaN or infinity ("NaN" parses as a float and compares false)."""
    try:
        if value is None or isinstance(value, bool) or str(value).strip() in ("", "-", "None", "null"):
            return None
        n = float(str(value).strip().rstrip("%"))
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _price(value):
    """A usable price: a real finite number above zero, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _clean(text, n=120):
    """Provider text is data: one line, short, never instructions."""
    return " ".join(str(text).split())[:n]


def _offset_hours(tz):
    """'UTC+01' -> 1.0; None when the provider didn't say."""
    m = re.fullmatch(r"\s*UTC\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\s*", tz or "")
    if not m:
        return None
    hours = int(m.group(2)) + int(m.group(3) or 0) / 60
    return hours if m.group(1) == "+" else -hours


# -- providers --------------------------------------------------------------

class ProviderError(Exception):
    pass


class AlphaVantage:
    name = "Alpha Vantage GLOBAL_QUOTE"
    url = "https://www.alphavantage.co/query"
    source_url = "https://www.alphavantage.co/documentation/#latestprice"
    freshness = "delayed"  # free tier: end of day
    reuse_hours = 6        # end-of-day data: don't spend the daily quota refetching it

    def __init__(self, key=None, get=None):
        self.key = key if key is not None else os.environ.get("ALPHAVANTAGE_API_KEY", "")
        self._get = get

    def _call(self, params):
        if not self.key:
            raise ProviderError("no ALPHAVANTAGE_API_KEY configured -- add ALPHAVANTAGE_API_KEY=<your key> "
                                "to .env (free key: alphavantage.co/support/#api-key)")
        get = self._get
        if get is None:
            import requests
            get = lambda u, p: requests.get(u, params=p, timeout=15).json()
        try:
            data = get(self.url, {**params, "apikey": self.key})
        except Exception as e:  # its text can carry the URL, key included
            raise ProviderError(f"request failed: {type(e).__name__}")
        if not isinstance(data, dict):
            raise ProviderError("unexpected response")
        for k in ("Note", "Information", "Error Message"):
            if k in data:  # rate limit or bad symbol: say so, don't parse
                raise ProviderError(f"{k}: {_clean(data[k]).replace(self.key, '<key>')}")
        return data

    def search(self, keywords):
        data = self._call({"function": "SYMBOL_SEARCH", "keywords": keywords})
        return [{"symbol": m.get("1. symbol"), "name": _clean(m.get("2. name") or "", 80),
                 "type": m.get("3. type"), "region": _clean(m.get("4. region") or "", 40),
                 "currency": m.get("8. currency"), "timezone": m.get("7. timezone")}
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
    source_url = "fixture (no URL)"
    freshness = "delayed"
    reuse_hours = 0

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
                               "region": "Fixture", "currency": "XXX", "timezone": "UTC+00"}]})
    return AlphaVantage()


# -- watchlist --------------------------------------------------------------

def watchlist():
    return _load("watchlist.json", {"instruments": []})["instruments"]


def list_text():
    try:
        items = watchlist()
    except StoreError as e:
        return f"Your watchlist file is unreadable, so I can't list it: {e}."
    return ("\n".join(f"{i['symbol']} ({i['name']}, {i['currency']}) alerts: "
                      f"{[f'{a['direction']} {a['level']:g}' for a in i['alerts']] or 'none'}"
                      for i in items) or "Your watchlist is empty.")


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
    try:
        with _locked():
            data = _load("watchlist.json", {"instruments": []})
            if any(i["symbol"] == m["symbol"] for i in data["instruments"]):
                return f"{m['symbol']} is already on your watchlist."
            data["instruments"].append({"symbol": m["symbol"], "name": m["name"], "region": m["region"],
                                        "currency": m["currency"], "utc_offset": _offset_hours(m.get("timezone")),
                                        "provider": prov.name, "added": _now().isoformat(timespec="seconds"),
                                        "alerts": []})
            _save("watchlist.json", data)
            saved = any(i["symbol"] == m["symbol"] for i in _load("watchlist.json", {"instruments": []})["instruments"])
    except StoreError as e:
        return f"I haven't added {m['symbol']}: {e}."
    if not saved:
        return f"I tried to add {m['symbol']} but it isn't in the saved watchlist."
    return f"Added {m['symbol']} ({m['name']}, {m['region']}, priced in {m['currency']}) to your watchlist."


def remove(symbol):
    try:
        with _locked():
            data = _load("watchlist.json", {"instruments": []})
            keep = [i for i in data["instruments"] if i["symbol"].upper() != (symbol or "").upper()]
            if len(keep) == len(data["instruments"]):
                return f"{symbol} isn't on your watchlist."
            data["instruments"] = keep
            _save("watchlist.json", data)
    except StoreError as e:
        return f"I haven't removed {symbol}: {e}."
    return f"Removed {symbol} from your watchlist."


def set_alert(symbol, direction, level, currency):
    """Fires when price >= level ("above") or price <= level ("below"),
    in the instrument's own currency -- a mismatch is refused, not converted."""
    if direction not in ("above", "below"):
        return "An alert is 'above' or 'below' a price."
    lvl = _num(level)
    if lvl is None or lvl <= 0:
        return "The alert needs a positive, finite price, like 300 or 2.5."
    try:
        with _locked():
            data = _load("watchlist.json", {"instruments": []})
            inst = next((i for i in data["instruments"] if i["symbol"].upper() == (symbol or "").upper()), None)
            if inst is None:
                return f"{symbol} isn't on your watchlist; add it first."
            if (currency or "").upper() != inst["currency"].upper():
                return (f"{inst['symbol']} is priced in {inst['currency']}, not {currency}. Give the level in "
                        f"{inst['currency']} (GBX is pence: 300 GBX = 3.00 GBP).")
            rule = {"direction": direction, "level": lvl, "currency": inst["currency"]}
            if rule not in inst["alerts"]:
                inst["alerts"].append(rule)
                _save("watchlist.json", data)
    except StoreError as e:
        return f"I haven't set that alert: {e}."
    return f"Alert set: {inst['symbol']} at or {direction} {lvl:g} {inst['currency']}."


# -- quotes, alerts, brief ---------------------------------------------------

def fetch(inst, prov, now=None):
    """One validated quote. Status is live/delayed/cached/unavailable.

    cached     the provider failed; this is the last good quote, with the
               time it was really retrieved. Never alerts.
    reused     (flag) a recent end-of-day quote not fetched again, to spare
               the quota; still "delayed", with its real retrieval time.
    """
    now = now or _now()
    try:
        cache = _load("quotes_cache.json", {})
    except StoreError:
        cache = {}  # only a cache: refetching costs a call, nothing is lost
    base = {"symbol": inst["symbol"], "currency": inst["currency"], "source": prov.name,
            "source_url": getattr(prov, "source_url", ""), "retrieved": now.isoformat(timespec="seconds")}
    old = cache.get(inst["symbol"])
    if old and old.get("source") == prov.name and getattr(prov, "reuse_hours", 0):
        age = now - dt.datetime.fromisoformat(old["retrieved"])
        if dt.timedelta(0) <= age < dt.timedelta(hours=prov.reuse_hours):
            return {**old, "status": prov.freshness, "reused": True}
    try:
        q = prov.quote(inst["symbol"])
    except ProviderError as e:
        if old:
            return {**old, "status": "cached", "problem": _clean(e)}
        return {**base, "status": "unavailable", "problem": _clean(e), "price": None,
                "previous_close": None, "change_percent": None, "market_date": None}
    quote = {**base, "price": _price(q.get("price")), "previous_close": _price(q.get("previous_close")),
             "change_percent": _num(q.get("change_percent")) if isinstance(q.get("change_percent"), (int, float)) else None,
             "market_date": q.get("market_date"), "status": prov.freshness}
    problems = []
    if quote["price"] is None:
        quote["status"] = "unavailable"
        problems.append("no valid price in the response" if q.get("price") is None else "the price in the response is invalid")
    # Exchange-local today, from the timezone the provider gave when it was
    # added; unknown -> the latest date anywhere, so nothing real is refused.
    # Unknown -> UTC+14 for "can't be in the future", UTC for staleness, so
    # neither check is ever stricter than the truth.
    offset = inst.get("utc_offset")
    local_today = (now + dt.timedelta(hours=offset if offset is not None else LATEST_UTC_OFFSET)).date()
    stale_today = (now + dt.timedelta(hours=offset or 0)).date()
    try:
        market = dt.date.fromisoformat(quote["market_date"])
    except (TypeError, ValueError):
        market = None
        problems.append("no valid market date in the response")
        quote["status"] = "unavailable"
    if market is not None:
        age = (stale_today - market).days
        if market > local_today:
            problems.append(f"market date {market} is after the exchange's today ({local_today})")
            quote["status"] = "unavailable"
        elif age > STALE_DAYS:
            quote["stale"] = True
            problems.append(f"market date is {age} days old")
    if problems:
        quote["problem"] = "; ".join(problems)
    if quote["status"] == "unavailable":
        quote["price"] = quote["previous_close"] = quote["change_percent"] = None
    else:
        cache[inst["symbol"]] = {k: v for k, v in quote.items() if k not in ("status", "problem", "reused")}
        with _locked():
            _save("quotes_cache.json", cache)
    return quote


def _event_id(inst, rule, quote):
    return f"{inst['symbol']}|{rule['direction']}|{rule['level']:g}|{quote['market_date']}"


def evaluate(inst, quote, now=None):
    """Alert events this quote triggers. Pure: nothing is recorded here.
    _acknowledge() records an event only after its brief is saved and
    verified, so a failed save can't swallow it."""
    now = now or _now()
    if quote["status"] not in ("live", "delayed") or quote.get("stale") or quote.get("price") is None:
        return []  # never alert on old, cached or missing data
    state = _load("alert_state.json", {})
    events = []
    for rule in inst.get("alerts", []):
        level = _num(rule.get("level"))
        if rule["currency"] != quote["currency"] or level is None or level <= 0:
            continue
        price = quote["price"]
        if not (price >= level if rule["direction"] == "above" else price <= level):
            continue
        eid = _event_id(inst, rule, quote)
        last = state.get(f"{inst['symbol']}|{rule['direction']}|{level:g}")
        if eid in state.get("delivered", []):
            continue
        if last and now - dt.datetime.fromisoformat(last["at"]) < dt.timedelta(hours=COOLDOWN_HOURS):
            continue
        events.append({**rule, "price": price, "id": eid, "symbol": inst["symbol"]})
    return events


def _acknowledge(events, now, brief_path=""):
    """Record delivered events: durable id + the rule's cooldown clock."""
    if not events:
        return
    with _locked():
        state = _load("alert_state.json", {})
        delivered = state.setdefault("delivered", [])
        for e in events:
            if e["id"] not in delivered:
                delivered.append(e["id"])
            state[e["id"].rsplit("|", 1)[0]] = {"market_date": e["id"].rsplit("|", 1)[1],
                                                "at": now.isoformat(timespec="seconds"),
                                                "brief": os.path.basename(brief_path)}
        state["delivered"] = delivered[-500:]
        _save("alert_state.json", state)


def _already_in_saved_brief(events, out_dir):
    """Events a saved, verified brief already carries (the run that wrote
    it died before acknowledging). Pending and failed briefs don't count."""
    found = []
    try:
        names = [n for n in os.listdir(out_dir) if re.fullmatch(r"brief-[\d-]+\.md", n)]
    except OSError:
        return found
    texts = []
    for n in names:
        try:
            with open(os.path.join(out_dir, n), encoding="utf-8") as f:
                texts.append(f.read())
        except OSError:
            pass
    for e in events:
        if any(f"[event {e['id']}]" in t for t in texts):
            found.append(e)
    return found


def _fmt(value, currency):
    return "unavailable" if value is None else f"{value:,.2f} {currency}"


def _obs_line(q, name):
    line = (f"- **{q['symbol']}** ({name}): {_fmt(q.get('price'), q['currency'])}; "
            f"previous close {_fmt(q.get('previous_close'), q['currency'])}; "
            f"status **{q['status']}**{' (stale)' if q.get('stale') else ''}; "
            f"market date {q.get('market_date') or 'unavailable'}; retrieved {q['retrieved']}; "
            f"source: {q['source']}" + (f" <{q['source_url']}>" if q.get("source_url") else ""))
    return line + (f"; problem: {q['problem']}" if q.get("problem") else "")


def _alert_line(e):
    return (f"- {e['symbol']} is {_fmt(e['price'], e['currency'])}, at or {e['direction']} "
            f"your {e['level']:g} {e['currency']} level. [event {e['id']}]")


_LEARN = ("Background, not advice: one company's price can swing a lot day to day (volatility); "
          "spreading money across many holdings (diversification) means no single swing decides "
          "the outcome; fees compound against you over years; and the longer the time horizon, "
          "the more room there is to ride out falls. None of this says what you should buy.")

last_observations = []  # the structured observations behind the last brief


def brief(prov=None, now=None, out_dir=None):
    """Fetch, validate, evaluate, write, verify, then acknowledge alerts.

    The brief is written as .pending, verified against the structured
    observations, renamed into place, and only then are its alerts
    recorded. A failure at any step leaves them to be delivered next run;
    a crash after the rename is caught next run by the event id in the
    saved brief. At-least-once, not exactly-once.
    """
    global last_observations
    prov, now = prov or provider(), now or _now()
    out_dir = out_dir or _path("briefs")
    try:
        items = watchlist()
    except StoreError as e:
        return f"I can't brief your watchlist: {e}."
    if not items:
        return "Your watchlist is empty, so there's nothing to brief. Add an instrument first, e.g. 'python invest.py add TSCO.LON'."
    obs, notes, events = [], [], []
    try:
        for inst in items:
            q = fetch(inst, prov, now)
            obs.append({**q, "name": inst["name"]})
            events += evaluate(inst, q, now)
            cp = q.get("change_percent")
            if q["status"] in ("live", "delayed") and not q.get("stale") and cp is not None:
                size = "a small" if abs(cp) < 1 else "a noticeable" if abs(cp) < 3 else "a large"
                notes.append(f"- {inst['symbol']} moved {cp:+.2f}% on its last trading day -- {size} one-day "
                             "move for a single share. The size alone doesn't say why it moved, and one day "
                             "says little about the long run.")
            elif q["status"] != "delayed" or q.get("stale"):
                notes.append(f"- {inst['symbol']}: no fresh data, so nothing to interpret.")
    except StoreError as e:
        return f"I can't brief your watchlist: {e}."
    recovered = _already_in_saved_brief(events, out_dir)
    if recovered:
        _acknowledge(recovered, now)
        events = [e for e in events if e not in recovered]
    last_observations = obs
    fixture = any(FIXTURE_LABEL in o["source"] for o in obs)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    text = "\n".join([
        f"# Watchlist brief -- {stamp}", "",
        *([f"> **{FIXTURE_LABEL}.** Every number below is made up for testing.", ""] if fixture else []),
        "## Observations (from the source named on each line)", "", *[_obs_line(o, o["name"]) for o in obs], "",
        "## Alerts", "", *([_alert_line(e) for e in events] or ["- None fired."]), "",
        "## Interpretation (mine, not a fact and not advice)", "", *(notes or ["- Nothing to interpret."]), "",
        "## Background", "", _LEARN, "",
    ])
    try:
        os.makedirs(out_dir, exist_ok=True)
        stem, n = os.path.join(out_dir, f"brief-{now.strftime('%Y%m%d-%H%M%S')}"), 1
        path = stem + ".md"
        while os.path.exists(path) or os.path.exists(path + ".pending"):
            n += 1
            path = f"{stem}-{n}.md"
        with open(path + ".pending", "x", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        return f"I couldn't save the brief ({type(e).__name__}), so no alert was marked as sent; I'll include it next time."
    problems = verify(path + ".pending", obs, events)
    if problems:
        os.replace(path + ".pending", path[:-3] + ".failed.md")
        return (f"The brief at {path[:-3]}.failed.md didn't match what was fetched, so no alert was marked as "
                "sent. The check found: " + "; ".join(problems) + ".")
    os.replace(path + ".pending", path)
    _acknowledge(events, now, path)
    summary = f" {len(items)} instrument(s), {len(events)} alert(s)."
    return (f"Watchlist brief saved to {path}." + summary
            + (" These are synthetic fixtures, not market data." if fixture else ""))


def verify(path, obs, events=()):
    """Read the saved brief back and check every observation and alert
    line exactly -- value, currency, date, status, source -- not just that
    a symbol appears."""
    try:
        with open(path, encoding="utf-8") as f:
            saved = f.read().splitlines()
    except OSError as e:
        return [f"the brief couldn't be read back ({type(e).__name__})"]
    problems = [f"{o['symbol']}: the saved line doesn't match what was fetched"
                for o in obs if _obs_line(o, o["name"]) not in saved]
    problems += [f"alert {e['id']} is missing or changed" for e in events if _alert_line(e) not in saved]
    heads = [line for line in saved if line.startswith("## ")]
    if heads[:2] != ["## Observations (from the source named on each line)", "## Alerts"] \
            or "## Interpretation (mine, not a fact and not advice)" not in heads:
        problems.append("observations and interpretation are not separated")
    return problems


def cancel_alert(symbol, direction, level):
    lvl = _num(level)
    try:
        with _locked():
            data = _load("watchlist.json", {"instruments": []})
            inst = next((i for i in data["instruments"] if i["symbol"].upper() == (symbol or "").upper()), None)
            if inst is None:
                return f"{symbol} isn't on your watchlist."
            keep = [a for a in inst["alerts"] if not (a["direction"] == direction and a["level"] == lvl)]
            if len(keep) == len(inst["alerts"]):
                return f"There's no {direction} {level} alert on {inst['symbol']}."
            inst["alerts"] = keep
            _save("watchlist.json", data)
    except StoreError as e:
        return f"I haven't cancelled it: {e}."
    return f"Cancelled the {inst['symbol']} alert at or {direction} {lvl:g} {inst['currency']}."


# -- chat: a few exact commands, no model call -------------------------------
# Watching only. "what is a watchlist" and "should i buy X" are not commands
# and go to the model. An ambiguous add is held as a pending choice tied to
# that request; only a listed symbol or its number answers it.

_SYM = r"([A-Za-z0-9][A-Za-z0-9.\-]{0,14})"
_CHAT = [
    ("search", re.compile(rf"^\s*(?:find|look\s+up|search(?:\s+for)?)\s+(?:the\s+)?(?:ticker|instrument|stock|share)s?\s+(?:for\s+)?(.+?)\s*[.?!]*$", re.I)),
    ("add", re.compile(rf"^\s*(?:please\s+)?(?:add|watch)\s+{_SYM}\s+(?:to|on)\s+my\s+watchlist\s*[.!]*$", re.I)),
    ("remove", re.compile(rf"^\s*(?:please\s+)?(?:remove|delete|drop)\s+{_SYM}\s+from\s+my\s+watchlist\s*[.!]*$", re.I)),
    ("alert", re.compile(rf"^\s*(?:set\s+an?\s+alert\s+(?:for|on)|alert\s+me\s+(?:if|when))\s+{_SYM}\s+(?:goes\s+|is\s+|gets\s+)?(above|below)\s+([0-9][0-9.,]*)\s*([A-Za-z]{{3}})?\s*[.!]*$", re.I)),
    ("cancel", re.compile(rf"^\s*(?:cancel|remove|delete)\s+(?:the\s+|my\s+)?{_SYM}\s+alert\s+(above|below)\s+([0-9][0-9.,]*)\s*[.!]*$", re.I)),
    ("alerts", re.compile(r"^\s*(?:list|show)\s+my\s+(?:price\s+)?alerts\s*[.?!]*$|^\s*what\s+alerts\s+do\s+i\s+have\s*\??$", re.I)),
]
PENDING_MINUTES = 10


def _pending_choice(text, now):
    try:
        pending = _load("pending.json", None)
    except StoreError:
        return None
    if not pending:
        return None
    with _locked():
        _save("pending.json", {})  # one answer, whatever it is
    if now - dt.datetime.fromisoformat(pending["at"]) > dt.timedelta(minutes=PENDING_MINUTES):
        return None
    pick = text.strip().rstrip(".").upper()
    options = pending["options"]
    chosen = (options[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(options)
              else pick if pick in options else None)
    return chosen


def chat(text, prov=None, now=None):
    """A reply for a watchlist command, or None if it isn't one."""
    now = now or _now()
    chosen = _pending_choice(text or "", now)
    if chosen:
        return add(chosen, prov)
    for kind, rx in _CHAT:
        m = rx.match(text or "")
        if not m:
            continue
        prov = prov or provider()
        if kind == "search":
            try:
                found = prov.search(m.group(1))
            except ProviderError as e:
                return f"I couldn't search the data provider: {e}."
            return ("No instrument matches that." if not found else "Matches: " + "; ".join(
                f"{f['symbol']} ({f['name']}, {f['region']}, {f['currency']})" for f in found[:5]) + ".")
        if kind == "add":
            reply = add(m.group(1), prov)
            if "isn't one exact instrument" in reply:
                try:
                    options = [f["symbol"].upper() for f in prov.search(m.group(1))][:5]
                except ProviderError:
                    return reply
                with _locked():
                    _save("pending.json", {"request": text, "options": options, "at": now.isoformat(timespec="seconds")})
                return (reply.replace(" Add the exact symbol you mean. Nothing added.", "") + " Which one? Say its symbol or "
                        "number (" + ", ".join(f"{n} = {o}" for n, o in enumerate(options, 1)) + "). Nothing added yet.")
            return reply
        if kind == "remove":
            return remove(m.group(1))
        if kind == "alert":
            sym, direction, level, cur = m.group(1), m.group(2).lower(), m.group(3).replace(",", ""), m.group(4)
            if not cur:
                return (f"Which currency is {level} in? Say it with the currency, e.g. 'alert me if {sym} goes "
                        f"{direction} {level} GBX' -- pence and pounds differ by 100x, so I won't guess.")
            return set_alert(sym, direction, level, cur.upper())
        if kind == "cancel":
            return cancel_alert(m.group(1), m.group(2).lower(), m.group(3).replace(",", ""))
        if kind == "alerts":
            try:
                rows = [f"{i['symbol']} {a['direction']} {a['level']:g} {a['currency']}"
                        for i in watchlist() for a in i["alerts"]]
            except StoreError as e:
                return f"I can't read your alerts: {e}."
            return "Your alerts: " + "; ".join(rows) + "." if rows else "You have no price alerts."
    return None


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
        print(list_text())
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
