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

Every value -- paths, provider URLs and timeouts, cache and staleness times,
alert cooldown, lock limits, the chat command patterns, the brief's wording
and size bands -- is in invest.json beside this file (AGENTS.md Rule 0),
loaded once at import and checked against invest_defaults.json (the same
values): a bad value logs one warning and its default is used. Nothing
here raises over a bad value.

    python invest.py search tesco
    python invest.py add TSCO.LON
    python invest.py alert TSCO.LON above 300 GBX
    python invest.py list | remove TSCO.LON
    python invest.py brief            # --fixtures for the synthetic provider
"""
import copy
import datetime as dt
import json
import logging
import math
import os
import re
import string
import sys
import tempfile
import threading
import time
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("denisse.invest")

# -- config (invest.json) ----------------------------------------------------
# invest_defaults.json holds the documented defaults -- the values invest.json
# ships with (scripts/test_invest_config.py keeps the two equal). They are
# used only for a key that is missing or invalid in invest.json.
DEFAULTS_PATH = os.path.splitext(os.path.abspath(__file__))[0] + "_defaults.json"


def _read_defaults():
    try:
        with open(DEFAULTS_PATH, encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        log.error("invest config: %s is not a JSON object; invest.json is used unchecked", DEFAULTS_PATH)
    except (OSError, ValueError) as e:
        log.error("invest config: %s unreadable (%s); invest.json is used unchecked", DEFAULTS_PATH, e)
    return {}


DEFAULTS = {k: v for k, v in _read_defaults().items() if not k.startswith("_")}
MAY_BE_ZERO = {"reuse_hours"}  # every other number must be above 0
SYM_SLOT = "{sym}"             # where chat.symbol goes in a chat pattern


def config_path():
    return os.environ.get("DENISSE_INVEST_CONFIG") or os.path.splitext(os.path.abspath(__file__))[0] + ".json"


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _groups(pattern, sym):
    """(capture-group count, None) for a chat pattern, or (None, why it can't be used)."""
    try:
        return re.compile(pattern.replace(SYM_SLOT, sym), re.I).groups, None
    except (re.error, TypeError, AttributeError) as e:
        return None, f"is not a valid regex ({type(e).__name__})"


DEMO_KEYS = ("search", "symbol", "name", "type", "region", "currency", "timezone")
DEMO_NUMBERS = ("price", "previous_close", "change_percent")


def _check(key, value, default):
    """(value, None) if `value` has the shape of `default`, else (None, why)."""
    if key == "demo_instruments":
        ok = isinstance(value, list) and all(
            isinstance(d, dict) and all(isinstance(d.get(k), str) and d[k].strip() for k in DEMO_KEYS)
            and all(_is_number(d.get(k)) for k in DEMO_NUMBERS) for d in value)
        return (value, None) if ok else (None, "must be a list of instruments, each with "
                                         + ", ".join(DEMO_KEYS + DEMO_NUMBERS))
    if key == "move_sizes":
        ok = (isinstance(value, list) and all(
            isinstance(b, dict) and _is_number(b.get("below")) and b["below"] > 0 and isinstance(b.get("word"), str)
            for b in value) and all(a["below"] < b["below"] for a, b in zip(value, value[1:])))
        return (value, None) if ok else (None, "must be a list of {below: number > 0, word: text}, smallest first")
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        if not _is_number(value):
            return None, "must be a number"
        if isinstance(default, int) and not float(value).is_integer():
            return None, "must be a whole number"
        if value < 0 or (value == 0 and key not in MAY_BE_ZERO):
            return None, "must not be negative" if key in MAY_BE_ZERO else "must be above 0"
        return (int(value) if isinstance(default, int) else float(value)), None
    if isinstance(default, str):
        return (value, None) if isinstance(value, str) and value.strip() else (None, "must be non-empty text")
    return value, None


def _merge(defaults, data, where):
    """`defaults` overlaid with every valid value in `data`; one warning per bad one."""
    out = copy.deepcopy(defaults)
    if not isinstance(data, dict):
        log.warning("invest config: %s is not an object; using its defaults", where or "the file")
        return out
    for key, value in data.items():
        name = f"{where}.{key}" if where else key
        if key.startswith("_"):
            continue
        if key not in defaults:
            log.warning("invest config: unknown key %s ignored", name)
        elif key == "patterns":
            out[key] = value  # checked in _check_patterns, once chat.symbol is known
        elif isinstance(defaults[key], dict):
            out[key] = _merge(defaults[key], value, name)
        else:
            good, why = _check(key, value, defaults[key])
            if why:
                log.warning("invest config: %s %s; using the default %r", name, why, defaults[key])
            else:
                out[key] = good
    return out


def _check_patterns(chat):
    """Every chat pattern compiles and keeps the default's capture groups."""
    n, why = _groups(chat["symbol"], "")
    if why or n != 1:
        log.warning("invest config: chat.symbol %s; using the default",
                    why or "must have exactly one capture group")
        chat["symbol"] = DEFAULTS["chat"]["symbol"]
    sym, given = chat["symbol"], chat["patterns"]
    pats = copy.deepcopy(DEFAULTS["chat"]["patterns"])
    if not isinstance(given, dict):
        log.warning("invest config: chat.patterns is not an object; using the defaults")
        given = {}
    for kind, pattern in given.items():
        if kind.startswith("_"):
            continue
        if kind not in pats:
            log.warning("invest config: unknown chat command chat.patterns.%s ignored", kind)
            continue
        want = _groups(pats[kind], sym)[0]
        got, why = _groups(pattern, sym) if isinstance(pattern, str) else (None, "must be text")
        if why or got != want:
            log.warning("invest config: chat.patterns.%s %s; using the default", kind,
                        why or f"must have {want} capture group(s), not {got}")
        else:
            pats[kind] = pattern
    chat["patterns"] = pats


def _fields(template):
    """The {field} names a reply template uses, or None if it isn't a valid template."""
    try:
        return {f.split(".")[0].split("[")[0] for _, f, _, _ in string.Formatter().parse(template) if f}
    except ValueError:
        return None


def _check_replies(replies, defaults, where="replies"):
    """A reply template may drop a field its default has, never add one:
    the code only passes the default's fields."""
    for key, default in defaults.items():
        name = f"{where}.{key}"
        if isinstance(default, dict):
            _check_replies(replies[key], default, name)
            continue
        got = _fields(replies[key])
        if got is None or not got <= _fields(default):
            log.warning("invest config: %s %s; using the default", name,
                        "is not a valid template" if got is None
                        else "uses " + ", ".join(sorted(got - _fields(default))) + " which it isn't given")
            replies[key] = default


def _fill(_where, _template, _default, **fields):
    """`_template` filled in; one that still fails (a bad format spec) falls back to `_default`."""
    try:
        return _template.format(**fields)
    except (KeyError, IndexError, ValueError, TypeError, AttributeError) as e:
        log.warning("invest config: %s can't be filled in (%s); using the default", _where, type(e).__name__)
        return _default.format(**fields)


def _say(_key, **fields):
    """The reply invest.json has for `_key` ("section.name" under replies), filled in."""
    section, item = _key.split(".")
    return _fill("replies." + _key, CONFIG["replies"][section][item],
                 DEFAULTS.get("replies", CONFIG["replies"])[section][item], **fields)


def load_config(path=None):
    """invest.json (or `path`) checked and merged over DEFAULTS. Never raises."""
    path = path or config_path()
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("invest config: %s unreadable (%s); using every default", path, type(e).__name__)
        data = {}
    if not DEFAULTS:  # no reference to check against (logged at import): take the file as written
        return data
    cfg = _merge(DEFAULTS, data, "")
    _check_patterns(cfg["chat"])
    _check_replies(cfg["replies"], DEFAULTS["replies"])
    return cfg


def configure(path=None):
    """Load invest.json (or `path`) and set the module's values from it.
    Called once at import; tests call it again with another file."""
    global CONFIG, DATA_DIR, FIXTURE_DIR, STALE_DAYS, COOLDOWN_HOURS, LATEST_UTC_OFFSET, PENDING_MINUTES, _CHAT
    global FIXTURE_LABEL
    CONFIG = load_config(path)
    # Relative to the repo; an absolute path is used as it is.
    DATA_DIR = os.path.join(BASE_DIR, os.path.normpath(CONFIG["paths"]["data_dir"]))
    # --fixtures keeps its own store, so a demo never lands on the real watchlist.
    FIXTURE_DIR = os.path.join(BASE_DIR, os.path.normpath(CONFIG["paths"]["fixture_dir"]))
    STALE_DAYS = CONFIG["quotes"]["stale_days"]
    COOLDOWN_HOURS = CONFIG["alerts"]["cooldown_hours"]
    LATEST_UTC_OFFSET = CONFIG["quotes"]["latest_utc_offset_hours"]
    PENDING_MINUTES = CONFIG["chat"]["pending_minutes"]
    sym = CONFIG["chat"]["symbol"]
    _CHAT = [(kind, re.compile(p.replace(SYM_SLOT, sym), re.I)) for kind, p in CONFIG["chat"]["patterns"].items()]
    AlphaVantage.configure(CONFIG["provider"])
    FIXTURE_LABEL = CONFIG["fixtures"]["label"]
    fx = CONFIG["fixtures"]
    FixtureProvider.name = _fill("fixtures.provider_name", fx["provider_name"],
                                 DEFAULTS.get("fixtures", fx)["provider_name"], label=FIXTURE_LABEL)
    FixtureProvider.source_url = CONFIG["fixtures"]["source_url"]
    return CONFIG


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
        raise StoreError(_say("store.unreadable", name=name, error=type(e).__name__))


def _when(value):
    """A timezone-aware ISO time, or None."""
    try:
        t = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else None


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _watchlist_shape(data):
    """Why the watchlist can't be used as it is, or None. Valid JSON in the
    wrong shape ({"instruments": null}) used to escape as a TypeError."""
    if not isinstance(data, dict) or not isinstance(data.get("instruments"), list):
        return _say("store.no_instruments")
    for n, i in enumerate(data["instruments"], 1):
        if not isinstance(i, dict):
            return _say("store.not_instrument", n=n)
        for k in ("symbol", "currency"):
            if not isinstance(i.get(k), str) or not i[k].strip():
                return _say("store.missing_field", n=n, field=k)
        if not isinstance(i.get("name"), str):  # may be empty: the provider doesn't always give one
            return _say("store.no_name", n=n)
        if i.get("utc_offset") is not None and not _finite(i["utc_offset"]):
            return _say("store.bad_offset", symbol=i["symbol"])
        if not isinstance(i.get("alerts"), list):
            return _say("store.no_alert_list", symbol=i["symbol"])
        for a in i["alerts"]:
            if not (isinstance(a, dict) and a.get("direction") in ("above", "below") and _finite(a.get("level"))
                    and a["level"] > 0 and isinstance(a.get("currency"), str)):
                return _say("store.bad_alert", symbol=i["symbol"])
    return None


def _alert_state_shape(data):
    if not isinstance(data, dict):
        return _say("store.not_alert_state")
    if not (isinstance(data.get("delivered", []), list) and all(isinstance(d, str) for d in data.get("delivered", []))):
        return _say("store.bad_delivered")
    if any(k != "delivered" and not (isinstance(v, dict) and _when(v.get("at"))) for k, v in data.items()):
        return _say("store.bad_sent_time")
    return None


def _pending_shape(data):
    if data in ({}, None):
        return None
    if not (isinstance(data, dict) and isinstance(data.get("question"), str) and _when(data.get("at"))
            and isinstance(data.get("options"), list) and data["options"]
            and all(isinstance(o, str) for o in data["options"])):
        return _say("store.bad_pending")
    return None


def _load_checked(name, default, shape):
    """_load, plus the structure every caller relies on. A file in the wrong
    shape is refused and left as it is -- never reset, never saved over."""
    data = _load(name, default)
    why = shape(data)
    if why:
        raise StoreError(_say("store.bad_shape", name=name, why=why, path=_path(name)))
    return data


def _load_watchlist():
    return _load_checked("watchlist.json", {"instruments": []}, _watchlist_shape)


def _save(name, data):
    if name == "watchlist.json" and _watchlist_shape(data):
        # Never write what every later read would refuse.
        raise StoreError(_say("store.would_be_invalid", name=name, why=_watchlist_shape(data)))
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
        for _ in range(CONFIG["store"]["lock_attempts"]):
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
                time.sleep(CONFIG["store"]["lock_wait_s"])
        else:
            raise StoreError(_say("store.busy"))
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


def _clean(text, n=None):
    """Provider text is data: one line, short, never instructions."""
    return " ".join(str(text).split())[:n or CONFIG["text_limits"]["provider_text"]]


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
    # name, url, source_url, reuse_hours (end-of-day data: don't spend the
    # daily quota refetching it), key_env, key_help and the request timeout
    # come from invest.json "provider", set by configure().
    freshness = "delayed"  # free tier: end of day

    @classmethod
    def configure(cls, p):
        cls.name, cls.url, cls.source_url, cls.reuse_hours = p["name"], p["url"], p["source_url"], p["reuse_hours"]
        cls.key_env, cls.key_help, cls.timeout = p["key_env"], p["key_help"], p["request_timeout_s"]

    def __init__(self, key=None, get=None):
        self.key = key if key is not None else os.environ.get(self.key_env, "")
        self._get = get

    def _call(self, params):
        if not self.key:
            raise ProviderError(_say("provider.no_key", key_env=self.key_env, key_help=self.key_help))
        get = self._get
        if get is None:
            import requests
            get = lambda u, p: requests.get(u, params=p, timeout=self.timeout).json()
        try:
            data = get(self.url, {**params, "apikey": self.key})
        except Exception as e:  # its text can carry the URL, key included
            raise ProviderError(_say("provider.request_failed", error=type(e).__name__))
        if not isinstance(data, dict):
            raise ProviderError(_say("provider.unexpected"))
        for k in ("Note", "Information", "Error Message"):
            if k in data:  # rate limit or bad symbol: say so, don't parse
                raise ProviderError(_say("provider.provider_said", field=k,
                                         text=_clean(data[k]).replace(self.key, "<key>")))
        return data

    def search(self, keywords):
        data = self._call({"function": "SYMBOL_SEARCH", "keywords": keywords})
        lim = CONFIG["text_limits"]
        return [{"symbol": m.get("1. symbol"), "name": _clean(m.get("2. name") or "", lim["name"]),
                 "type": m.get("3. type"), "region": _clean(m.get("4. region") or "", lim["region"]),
                 "currency": m.get("8. currency"), "timezone": m.get("7. timezone")}
                for m in data.get("bestMatches") or [] if m.get("1. symbol")]

    def quote(self, symbol):
        q = self._call({"function": "GLOBAL_QUOTE", "symbol": symbol}).get("Global Quote") or {}
        if not q or q.get("01. symbol", "").upper() != symbol.upper():
            raise ProviderError(_say("provider.no_quote", symbol=symbol))
        return {"price": _num(q.get("05. price")), "previous_close": _num(q.get("08. previous close")),
                "change_percent": _num(q.get("10. change percent")),
                "market_date": q.get("07. latest trading day") or None}


class FixtureProvider:
    """Synthetic quotes for tests and demos. Never market data.
    name and source_url come from invest.json "fixtures", set by configure()."""
    freshness = "delayed"
    reuse_hours = 0

    def __init__(self, quotes=None, matches=None, fail=False):
        self.quotes, self.matches, self.fail = quotes or {}, matches or {}, fail

    def search(self, keywords):
        if self.fail:
            raise ProviderError(_say("provider.fixture_fail"))
        key = keywords.upper()
        return list(self.matches.get(key) or self.matches.get(key.split(".")[0], []))

    def quote(self, symbol):
        if self.fail or symbol not in self.quotes:
            raise ProviderError(_say("provider.fixture_fail") if self.fail else _say("provider.no_quote", symbol=symbol))
        return dict(self.quotes[symbol])


def provider(fixtures=False):
    if fixtures:
        today = dt.date.today().isoformat()
        quotes, matches = {}, {}
        for d in CONFIG["fixtures"]["demo_instruments"]:
            quotes[d["symbol"]] = {**{k: d[k] for k in DEMO_NUMBERS}, "market_date": today}
            matches.setdefault(d["search"].upper(), []).append({k: d[k] for k in DEMO_KEYS if k != "search"})
        return FixtureProvider(quotes=quotes, matches=matches)
    return AlphaVantage()


configure()  # once, at import (after AlphaVantage, which it sets up)


# -- watchlist --------------------------------------------------------------

def watchlist():
    return _load_watchlist()["instruments"]


def list_text():
    try:
        items = watchlist()
    except StoreError as e:
        return _say("watchlist.list_unreadable", error=e)
    return ("\n".join(_say("watchlist.list_line", symbol=i["symbol"], name=i["name"], currency=i["currency"],
                           alerts=str([_say("watchlist.list_alert", direction=a["direction"], level=a["level"])
                                       for a in i["alerts"]] or _say("watchlist.list_no_alerts")))
                      for i in items) or _say("watchlist.list_empty"))


def _option(m):
    return _say("watchlist.option", symbol=m["symbol"], name=m["name"], region=m["region"], currency=m["currency"])


def add(query, prov=None, ask=None):
    """Add one instrument. Refuses to choose between several matches; given
    a list as `ask`, it also gets [the ambiguity, without its ending; the
    symbols offered], so chat can ask which one without matching words."""
    prov = prov or provider()
    query = (query or "").strip()
    if not query:
        return _say("watchlist.add_no_query")
    try:
        matches = prov.search(query)
    except ProviderError as e:
        return _say("watchlist.add_check_failed", query=query, error=e)
    exact = [m for m in matches if (m["symbol"] or "").upper() == query.upper()]
    if len(exact) != 1:
        if not matches:
            return _say("watchlist.add_no_match", query=query)
        shown = matches[:CONFIG["chat"]["options_shown"]]
        ambiguous = _say("watchlist.add_ambiguous", query=query, options="; ".join(_option(m) for m in shown))
        if ask is not None:  # chat: the caller asks which one and holds the choice
            ask.extend([ambiguous, [(m["symbol"] or "").upper() for m in shown]])
        return ambiguous + _say("watchlist.add_ambiguous_end")
    m = exact[0]
    if not m.get("currency"):
        return _say("watchlist.add_no_currency", symbol=m["symbol"])
    try:
        with _locked():
            data = _load_watchlist()
            if any(i["symbol"] == m["symbol"] for i in data["instruments"]):
                return _say("watchlist.add_already", symbol=m["symbol"])
            data["instruments"].append({"symbol": m["symbol"], "name": m["name"], "region": m["region"],
                                        "currency": m["currency"], "utc_offset": _offset_hours(m.get("timezone")),
                                        "provider": prov.name, "added": _now().isoformat(timespec="seconds"),
                                        "alerts": []})
            _save("watchlist.json", data)
            saved = any(i["symbol"] == m["symbol"] for i in _load_watchlist()["instruments"])
    except StoreError as e:
        return _say("watchlist.add_failed", symbol=m["symbol"], error=e)
    if not saved:
        return _say("watchlist.add_not_saved", symbol=m["symbol"])
    return _say("watchlist.added", symbol=m["symbol"], name=m["name"], region=m["region"], currency=m["currency"])


def remove(symbol):
    try:
        with _locked():
            data = _load_watchlist()
            keep = [i for i in data["instruments"] if i["symbol"].upper() != (symbol or "").upper()]
            if len(keep) == len(data["instruments"]):
                return _say("watchlist.remove_missing", symbol=symbol)
            data["instruments"] = keep
            _save("watchlist.json", data)
    except StoreError as e:
        return _say("watchlist.remove_failed", symbol=symbol, error=e)
    return _say("watchlist.removed", symbol=symbol)


def set_alert(symbol, direction, level, currency):
    """Fires when price >= level ("above") or price <= level ("below"),
    in the instrument's own currency -- a mismatch is refused, not converted."""
    if direction not in ("above", "below"):
        return _say("alerts.bad_direction")
    lvl = _num(level)
    if lvl is None or lvl <= 0:
        return _say("alerts.bad_level")
    try:
        with _locked():
            data = _load_watchlist()
            inst = next((i for i in data["instruments"] if i["symbol"].upper() == (symbol or "").upper()), None)
            if inst is None:
                return _say("alerts.not_watched", symbol=symbol)
            if (currency or "").upper() != inst["currency"].upper():
                return _say("alerts.wrong_currency", symbol=inst["symbol"], currency=inst["currency"],
                            given=currency, hint=CONFIG["alerts"]["currency_hint"])
            rule = {"direction": direction, "level": lvl, "currency": inst["currency"]}
            if rule not in inst["alerts"]:
                inst["alerts"].append(rule)
                _save("watchlist.json", data)
    except StoreError as e:
        return _say("alerts.set_failed", error=e)
    return _say("alerts.set", symbol=inst["symbol"], direction=direction, level=lvl, currency=inst["currency"])


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
    if not isinstance(cache, dict):
        cache = {}
    old = cache.get(inst["symbol"])
    retrieved = _when(old.get("retrieved")) if isinstance(old, dict) else None
    if not (retrieved and retrieved <= now and old.get("symbol") == inst["symbol"]
            and old.get("currency") == inst["currency"] and old.get("source") == prov.name):
        old = None  # malformed, another provider or instrument, or "retrieved" in the future
    if old and now - retrieved < dt.timedelta(hours=getattr(prov, "reuse_hours", 0)):
        # Revalidated against today's clock, not trusted from when it was
        # saved: a quote fetched at 23:00 can be stale by 01:00. Stale or
        # invalid now -> ask the provider again.
        q = _observe(inst, prov, old, now, old["retrieved"], prov.freshness)
        if q["status"] != "unavailable" and not q.get("stale"):
            return {**q, "reused": True}
    try:
        raw = prov.quote(inst["symbol"])
    except ProviderError as e:
        if old:
            q = _observe(inst, prov, old, now, old["retrieved"], "cached")
            if q["status"] == "cached":
                return {**q, "problem": "; ".join([_clean(e)] + ([q["problem"]] if q.get("problem") else []))}
        return {**_observe(inst, prov, {}, now, now.isoformat(timespec="seconds"), "unavailable"),
                "problem": _clean(e)}
    quote = _observe(inst, prov, raw if isinstance(raw, dict) else {}, now,
                     now.isoformat(timespec="seconds"), prov.freshness)
    if quote["status"] != "unavailable":
        cache[inst["symbol"]] = {k: v for k, v in quote.items() if k not in ("status", "problem", "reused", "stale")}
        with _locked():
            _save("quotes_cache.json", cache)
    return quote


def _observe(inst, prov, raw, now, retrieved, status):
    """One quote checked against the clock `now` -- the same checks for a
    fresh response, a reused one and a cached fallback. Invalid ->
    "unavailable" with its numbers removed; too old -> stale."""
    quote = {"symbol": inst["symbol"], "currency": inst["currency"], "source": prov.name,
             "source_url": getattr(prov, "source_url", ""), "retrieved": retrieved,
             "price": _price(raw.get("price")), "previous_close": _price(raw.get("previous_close")),
             "change_percent": float(raw["change_percent"]) if _finite(raw.get("change_percent")) else None,
             "market_date": raw.get("market_date"), "status": status}
    if status == "unavailable":
        quote["price"] = quote["previous_close"] = quote["change_percent"] = quote["market_date"] = None
        return quote
    problems = []
    if quote["price"] is None:
        quote["status"] = "unavailable"
        problems.append(_say("provider.no_price") if raw.get("price") is None else _say("provider.bad_price"))
    # Exchange-local today, from the timezone the provider gave when it was
    # added. Unknown -> UTC+14 for "can't be in the future", UTC for
    # staleness, so neither check is ever stricter than the truth.
    offset = inst.get("utc_offset")
    local_today = (now + dt.timedelta(hours=offset if offset is not None else LATEST_UTC_OFFSET)).date()
    stale_today = (now + dt.timedelta(hours=offset or 0)).date()
    try:
        market = dt.date.fromisoformat(quote["market_date"])
    except (TypeError, ValueError):
        market = None
        problems.append(_say("provider.no_market_date"))
        quote["status"] = "unavailable"
    if market is not None:
        age = (stale_today - market).days
        if market > local_today:
            problems.append(_say("provider.future_date", market=market, today=local_today))
            quote["status"] = "unavailable"
        elif age > STALE_DAYS:
            quote["stale"] = True
            problems.append(_say("provider.old_date", age=age))
    if problems:
        quote["problem"] = "; ".join(problems)
    if quote["status"] == "unavailable":
        quote["price"] = quote["previous_close"] = quote["change_percent"] = None
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
    state = _load_checked("alert_state.json", {}, _alert_state_shape)
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
        state = _load_checked("alert_state.json", {}, _alert_state_shape)
        delivered = state.setdefault("delivered", [])
        for e in events:
            if e["id"] not in delivered:
                delivered.append(e["id"])
            state[e["id"].rsplit("|", 1)[0]] = {"market_date": e["id"].rsplit("|", 1)[1],
                                                "at": now.isoformat(timespec="seconds"),
                                                "brief": os.path.basename(brief_path)}
        state["delivered"] = delivered[-CONFIG["alerts"]["delivered_kept"]:]
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
        if any(EVENT_MARK.format(e["id"]) in t for t in texts):
            found.append(e)
    return found


def _fmt(value, currency):
    return _say("brief.unavailable") if value is None else _say("brief.money", value=value, currency=currency)


def _obs_line(q, name):
    return _say("brief.obs_line", symbol=q["symbol"], name=name, price=_fmt(q.get("price"), q["currency"]),
                previous_close=_fmt(q.get("previous_close"), q["currency"]), status=q["status"],
                stale=_say("brief.obs_stale") if q.get("stale") else "",
                market_date=q.get("market_date") or _say("brief.unavailable"), retrieved=q["retrieved"],
                source=q["source"],
                source_url=_say("brief.obs_source_url", url=q["source_url"]) if q.get("source_url") else "",
                problem=_say("brief.obs_problem", problem=q["problem"]) if q.get("problem") else "")


EVENT_MARK = "[event {}]"  # how a saved brief carries an alert's id; read back by _already_in_saved_brief


def _alert_line(e):
    return (_say("brief.alert_line", symbol=e["symbol"], price=_fmt(e["price"], e["currency"]),
                 direction=e["direction"], level=e["level"], currency=e["currency"])
            + " " + EVENT_MARK.format(e["id"]))


def _move_size(cp):
    """The word for a one-day move of cp percent (invest.json brief.move_sizes)."""
    b = CONFIG["brief"]
    return next((s["word"] for s in b["move_sizes"] if abs(cp) < s["below"]), b["move_largest"])


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
        return _say("brief.cant_brief", error=e)
    if not items:
        return _say("brief.empty")
    obs, notes, events = [], [], []
    try:
        for inst in items:
            q = fetch(inst, prov, now)
            obs.append({**q, "name": inst["name"]})
            events += evaluate(inst, q, now)
            cp = q.get("change_percent")
            if q["status"] in ("live", "delayed") and not q.get("stale") and cp is not None:
                size = _move_size(cp)
                notes.append(_say("brief.move_note", symbol=inst["symbol"], change=cp, size=size))
            elif q["status"] != "delayed" or q.get("stale"):
                notes.append(_say("brief.no_fresh_data", symbol=inst["symbol"]))
    except StoreError as e:
        return _say("brief.cant_brief", error=e)
    recovered = _already_in_saved_brief(events, out_dir)
    if recovered:
        try:
            _acknowledge(recovered, now)
        except StoreError as e:
            return _say("brief.cant_brief", error=e)
        events = [e for e in events if e not in recovered]
    last_observations = obs
    fixture = any(FIXTURE_LABEL in o["source"] for o in obs)
    stamp = now.strftime(CONFIG["replies"]["brief"]["stamp_format"])
    head = CONFIG["brief"]["headings"]
    text = "\n".join([
        _say("brief.title", stamp=stamp), "",
        *([_say("brief.fixture_banner", label=FIXTURE_LABEL), ""] if fixture else []),
        head["observations"], "", *[_obs_line(o, o["name"]) for o in obs], "",
        head["alerts"], "", *([_alert_line(e) for e in events] or [_say("brief.no_alerts")]), "",
        head["interpretation"], "", *(notes or [_say("brief.nothing_to_interpret")]), "",
        head["background"], "", CONFIG["brief"]["background"], "",
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
        return _say("brief.save_failed", error=type(e).__name__)
    problems = verify(path + ".pending", obs, events)
    if problems:
        os.replace(path + ".pending", path[:-3] + ".failed.md")
        return _say("brief.verify_failed", path=path[:-3] + ".failed.md", problems="; ".join(problems))
    os.replace(path + ".pending", path)
    try:
        _acknowledge(events, now, path)
    except StoreError as e:  # the saved brief carries the event ids; next run recovers them
        return _say("brief.ack_failed", path=path, error=e)
    return (_chat_summary(obs, events, fixture)
            + _say("brief.saved", path=path, instruments=len(items), alerts=len(events))
            + (_say("brief.saved_fixture") if fixture else ""))


def _chat_summary(obs, events, fixture, limit=None):
    """The brief in chat, from the same observations the saved report was
    verified against. Bounded, and it never gives a reason for a move."""
    limit = limit or CONFIG["chat"]["summary_limit"]
    lines = [_say("brief.summary_title", fixture=_say("brief.summary_fixture", label=FIXTURE_LABEL) if fixture else "")]
    for o in obs[:limit]:
        if o.get("price") is None:
            lines.append(_say("brief.summary_no_price", symbol=o["symbol"], problem=_clean(
                o.get("problem") or o["status"], CONFIG["text_limits"]["problem_in_chat"])))
            continue
        when = _when(o["retrieved"])
        when = (when.astimezone(dt.timezone.utc).strftime(CONFIG["replies"]["brief"]["summary_when_format"])
                if when else o["retrieved"])
        fresh = ((_say("brief.summary_stale") if o.get("stale") else "")
                 + CONFIG["brief"]["freshness_words"].get(o["status"], o["status"]))
        lines.append(_say("brief.summary_line", symbol=o["symbol"], price=_fmt(o["price"], o["currency"]),
                          market_date=o["market_date"], fresh=fresh, when=when))
    if len(obs) > limit:
        lines.append(_say("brief.summary_more", n=len(obs) - limit))
    if events:
        lines.append(_say("brief.summary_alerts", alerts="; ".join(
            _say("brief.summary_alert", symbol=e["symbol"], price=_fmt(e["price"], e["currency"]),
                 direction=e["direction"], level=e["level"], currency=e["currency"]) for e in events[:limit]),
            more=_say("brief.summary_alerts_more", n=len(events) - limit) if len(events) > limit else ""))
    else:
        lines.append(_say("brief.summary_no_alerts"))
    return "\n".join(lines)


def verify(path, obs, events=()):
    """Read the saved brief back and check every observation and alert
    line exactly -- value, currency, date, status, source -- not just that
    a symbol appears."""
    try:
        with open(path, encoding="utf-8") as f:
            saved = f.read().splitlines()
    except OSError as e:
        return [_say("brief.check_unreadable", error=type(e).__name__)]
    problems = [_say("brief.check_line", symbol=o["symbol"]) for o in obs if _obs_line(o, o["name"]) not in saved]
    problems += [_say("brief.check_alert", id=e["id"]) for e in events if _alert_line(e) not in saved]
    head = CONFIG["brief"]["headings"]
    heads = [line for line in saved if line in head.values()]
    if heads[:2] != [head["observations"], head["alerts"]] or head["interpretation"] not in heads:
        problems.append(_say("brief.check_sections"))
    return problems


def cancel_alert(symbol, direction, level):
    lvl = _num(level)
    try:
        with _locked():
            data = _load_watchlist()
            inst = next((i for i in data["instruments"] if i["symbol"].upper() == (symbol or "").upper()), None)
            if inst is None:
                return _say("alerts.cancel_not_watched", symbol=symbol)
            keep = [a for a in inst["alerts"] if not (a["direction"] == direction and a["level"] == lvl)]
            if len(keep) == len(inst["alerts"]):
                return _say("alerts.cancel_missing", direction=direction, level=level, symbol=inst["symbol"])
            inst["alerts"] = keep
            _save("watchlist.json", data)
    except StoreError as e:
        return _say("alerts.cancel_failed", error=e)
    return _say("alerts.cancelled", symbol=inst["symbol"], direction=direction, level=lvl, currency=inst["currency"])


# -- chat: a few exact commands, no model call -------------------------------
# Watching only. "what is a watchlist" and "should i buy X" are not commands
# and go to the model. An ambiguous add is held as a pending choice tied to
# that request; only a listed symbol or its number answers it. The command
# patterns (_CHAT) and PENDING_MINUTES come from invest.json "chat", set by
# configure(): they are the grammar of a few exact commands that write to
# the watchlist, not a guess at meaning -- anything else is the model's.


def _pending_choice(text, now, last_said=None):
    """The symbol this message picks from the open question, or None.

    Taken under the lock -- read and cleared in one step -- so whatever
    this message says, and however many arrive at once, the choice answers
    only one. `last_said` is her previous reply in this conversation: a
    choice only answers the question she has just asked. Without that,
    "thanks" -> "Any time." (a reply that never reaches chat()) left the
    choice open, and a later "1" added an instrument (2026-10-30 review)."""
    if not os.path.exists(_path("pending.json")):
        return None  # the usual case: no lock, and no store created for an ordinary message
    try:
        with _locked():
            pending = _load_checked("pending.json", None, _pending_shape)
            if not pending:
                return None
            _save("pending.json", {})  # one answer, whatever it is
    except StoreError:
        return None
    age = now - _when(pending["at"])
    if not dt.timedelta(0) <= age <= dt.timedelta(minutes=PENDING_MINUTES):
        return None
    if last_said is not None and " ".join(last_said.split()) != " ".join(pending["question"].split()):
        return None  # something else was said since she asked
    pick = text.strip().rstrip(".").upper()
    options = pending["options"]
    chosen = (options[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(options)
              else pick if pick in options else None)
    return chosen


def chat(text, prov=None, now=None, last_said=None):
    """A reply for a watchlist command, or None if it isn't one.
    `last_said`: her previous reply, so a pending choice answers only the
    question just asked (the brain passes it; None skips that check)."""
    now = now or _now()
    chosen = _pending_choice(text or "", now, last_said)
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
                return _say("search.failed", error=e)
            return (_say("search.none") if not found else _say("search.matches", matches="; ".join(
                _option(f) for f in found[:CONFIG["chat"]["options_shown"]])))
        if kind == "add":
            ask = []
            reply = add(m.group(1), prov, ask)
            if ask:  # several matches: ask which one rather than guess
                ambiguous, options = ask
                question = ambiguous + _say("watchlist.add_ambiguous_ask", numbered=", ".join(
                    _say("watchlist.numbered_option", n=n, symbol=o) for n, o in enumerate(options, 1)))
                try:
                    with _locked():  # replaces any earlier question
                        _save("pending.json", {"request": text, "question": question, "options": options,
                                               "at": now.isoformat(timespec="seconds")})
                except StoreError:
                    return reply
                return question
            return reply
        if kind == "remove":
            return remove(m.group(1))
        if kind == "alert":
            sym, direction, level, cur = m.group(1), m.group(2).lower(), m.group(3).replace(",", ""), m.group(4)
            if not cur:
                return _say("alerts.which_currency", level=level, symbol=sym, direction=direction,
                            example=CONFIG["alerts"]["example_currency"], warning=CONFIG["alerts"]["currency_warning"])
            return set_alert(sym, direction, level, cur.upper())
        if kind == "cancel":
            return cancel_alert(m.group(1), m.group(2).lower(), m.group(3).replace(",", ""))
        if kind == "alerts":
            try:
                rows = [_say("alerts.list_row", symbol=i["symbol"], direction=a["direction"], level=a["level"],
                             currency=a["currency"]) for i in watchlist() for a in i["alerts"]]
            except StoreError as e:
                return _say("alerts.list_unreadable", error=e)
            return _say("alerts.list", alerts="; ".join(rows)) if rows else _say("alerts.list_none")
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
                print(_say("search.cli_row", symbol=m["symbol"], region=m["region"], name=m["name"],
                           currency=m["currency"] or _say("search.cli_unknown_currency")))
        except ProviderError as e:
            print(_say("search.cli_failed", error=e))
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
