"""Insiders buying: open-market purchases by officers and directors (SEC Form 4).

Every value (user agent, URLs, codes, roles, thresholds, rate limit, cache path,
wording) comes from the markets config, `insiders` block, documented in
markets/README.md. Information only: this lists filings, it does not judge them.

`refresh()` is the only function that touches the network, and only when
`watchlist.edgar_user_agent` is set. Everything else reads the cache under
.review/. Tests pass their own `fetch` and `sleep`.
"""
from __future__ import annotations

import glob
import json
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

Fetch = Callable[[str, str], bytes]


# -- config -------------------------------------------------------------------
def settings(config: dict[str, Any]) -> dict[str, Any]:
    return dict((config or {}).get("insiders") or {})


def user_agent(config: dict[str, Any]) -> str:
    return str(((config or {}).get("watchlist") or {}).get("edgar_user_agent") or "").strip()


def _today() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# -- cache --------------------------------------------------------------------
def _cache_path(s: dict[str, Any]) -> Path:
    return Path(str(s.get("cache_path") or ""))


def load_cache(s: dict[str, Any]) -> dict[str, Any]:
    p = _cache_path(s)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("filings", {})
    data.setdefault("ticker_map", {})
    return data


def save_cache(s: dict[str, Any], data: dict[str, Any]) -> Path:
    p = _cache_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


# -- which symbols ------------------------------------------------------------
def _split(entry: str) -> tuple[str, str]:
    value = str(entry or "").strip().rstrip(",;")
    if ":" not in value:
        return "", value
    prefix, rest = value.split(":", 1)
    return prefix.strip().upper(), rest.strip()


def _export_text(config: dict[str, Any]) -> str | None:
    wl = (config or {}).get("watchlist") or {}
    paths: list[str] = []
    pattern = wl.get("tradingview_watchlist_glob")
    if isinstance(pattern, str) and pattern.strip():
        paths += glob.glob(pattern, recursive=True)
    paths += [str(f) for f in (wl.get("tradingview_watchlist_files") or []) if f]
    best = None
    for item in dict.fromkeys(paths):
        try:
            p = Path(item)
            if p.is_file() and (best is None or p.stat().st_mtime_ns > best[0]):
                best = (p.stat().st_mtime_ns, p)
        except OSError:
            continue
    if best is None:
        return None
    try:
        return best[1].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def us_symbols(config: dict[str, Any], export_text: str | None = None) -> list[str]:
    """US tickers on the watchlist: every `###` section of the newest TradingView export,
    or the config groups (through symbol_map) when there is no export. A symbol counts as
    US when its TradingView exchange prefix is in insiders.us_exchanges."""
    s = settings(config)
    us = {str(x).upper() for x in s.get("us_exchanges") or []}
    replace = dict(s.get("ticker_replace") or {})
    text = export_text if export_text is not None else _export_text(config)
    entries: list[str] = []
    if text:
        for line in text.splitlines():
            entries += [p.strip() for p in line.split(",") if p.strip() and not p.strip().startswith("###")]
    else:
        wl = (config or {}).get("watchlist") or {}
        mapping = wl.get("symbol_map") or {}
        for members in (wl.get("groups") or {}).values():
            entries += [str(mapping.get(m, m)) for m in members]
    out: list[str] = []
    for entry in entries:
        prefix, rest = _split(entry)
        if prefix in us and rest:
            ticker = rest.upper()
            for old, new in replace.items():
                ticker = ticker.replace(old, new)
            out.append(ticker)
    return list(dict.fromkeys(out))


# -- Form 4 parsing -------------------------------------------------------------
def _text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    found = node.find(path)
    if found is None:
        return ""
    value = found.find("value")
    return ((value.text if value is not None else found.text) or "").strip()


def _flag(node: ET.Element | None, path: str, truthy: set[str]) -> bool:
    return _text(node, path).lower() in truthy


def _num(value: str) -> float | None:
    try:
        return float(value.replace(",", ""))
    except (AttributeError, ValueError):
        return None


def parse_form4(xml_text: str | bytes, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows for the transactions in one Form 4 that match insiders.transaction_codes /
    acquired_disposed, filed by an owner whose role is in insiders.roles."""
    s = settings(config)
    codes = {str(c).upper() for c in s.get("transaction_codes") or []}
    acq = {str(c).upper() for c in s.get("acquired_disposed") or []}
    wanted_roles = {str(r) for r in s.get("roles") or []}
    words = s.get("role_words") or {}
    truthy = {str(t).lower() for t in s.get("truthy") or []}
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, TypeError, ValueError):
        return []
    issuer = _text(root, "issuer/issuerName")
    symbol = _text(root, "issuer/issuerTradingSymbol").upper()
    owners = []
    for owner in root.findall("reportingOwner"):
        rel = owner.find("reportingOwnerRelationship")
        roles = []
        if "officer" in wanted_roles and _flag(rel, "isOfficer", truthy):
            roles.append(_text(rel, "officerTitle") or words.get("officer", "officer"))
        if "director" in wanted_roles and _flag(rel, "isDirector", truthy):
            roles.append(words.get("director", "director"))
        if roles:
            owners.append({"who": _text(owner, "reportingOwnerId/rptOwnerName"), "role": ", ".join(roles)})
    if not owners:
        return []
    rows = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding/transactionCode").upper()
        ad = _text(tx, "transactionAmounts/transactionAcquiredDisposedCode").upper()
        if code not in codes or (acq and ad not in acq):
            continue
        shares = _num(_text(tx, "transactionAmounts/transactionShares"))
        price = _num(_text(tx, "transactionAmounts/transactionPricePerShare"))
        value = round(shares * price, 2) if shares is not None and price is not None else None
        for o in owners:
            rows.append({"ticker": symbol, "issuer": issuer, "who": o["who"], "role": o["role"],
                         "date": _text(tx, "transactionDate")[:10], "code": code,
                         "shares": shares, "price": price, "value": value})
    return rows


# -- EDGAR ----------------------------------------------------------------------
def http_fetch(timeout: float) -> Fetch:
    def fetch(url: str, agent: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": agent})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    return fetch


class _Limiter:
    """SEC fair access: at least min_request_interval_s between requests and at most
    max_requests_per_run per refresh."""

    def __init__(self, s: dict[str, Any], fetch: Fetch, agent: str, sleep: Callable[[float], None],
                 clock: Callable[[], float]):
        self.interval = float(s.get("min_request_interval_s") or 0)
        self.left = int(s.get("max_requests_per_run") or 0)
        self.fetch, self.agent, self.sleep, self.clock = fetch, agent, sleep, clock
        self.last: float | None = None
        self.count = 0

    def get(self, url: str) -> bytes:
        if self.count >= self.left:
            raise _Budget()
        if self.last is not None:
            wait = self.interval - (self.clock() - self.last)
            if wait > 0:
                self.sleep(wait)
        self.count += 1
        try:
            return self.fetch(url, self.agent)
        finally:
            self.last = self.clock()


class _Budget(Exception):
    pass


def _raw_document(primary: str) -> str:
    # The submissions index points at the rendered page (xslF345X05/form4.xml); the
    # filing's own XML is the same name at the top of the accession folder.
    return primary.rsplit("/", 1)[-1]


def refresh(config: dict[str, Any], fetch: Fetch | None = None, sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic, export_text: str | None = None,
            now: datetime | None = None) -> dict[str, Any]:
    """Fetch new Form 4 filings for the US watchlist symbols into the cache. Without a user
    agent it does nothing and records why (shown in the panel)."""
    s = settings(config)
    words = s.get("words") or {}
    cache = load_cache(s)
    now = now or _today()
    agent = user_agent(config)
    if not s.get("enabled", True):
        cache["message"] = words.get("disabled", "")
        save_cache(s, cache)
        return cache
    if not agent:
        cache["message"] = words.get("no_user_agent", "")
        save_cache(s, cache)
        return cache
    lim = _Limiter(s, fetch or http_fetch(float(s.get("timeout_s") or 20)), agent, sleep, clock)
    forms = {str(f) for f in s.get("forms") or []}
    since = (now - timedelta(days=int(s.get("lookback_days") or 0))).strftime("%Y-%m-%d")
    per_symbol = int(s.get("max_filings_per_symbol") or 0)
    errors: list[str] = []
    try:
        tmap = cache.get("ticker_map") or {}
        age = int(s.get("ticker_map_max_age_days") or 0)
        fetched = str(tmap.get("fetched") or "")
        if not tmap.get("map") or fetched < (now - timedelta(days=age)).strftime("%Y-%m-%d"):
            raw = json.loads(lim.get(str(s["ticker_map_url"])))
            rows = raw.values() if isinstance(raw, dict) else raw
            tmap = {"fetched": now.strftime("%Y-%m-%d"),
                    "map": {str(r["ticker"]).upper(): str(r["cik_str"]).zfill(10) for r in rows
                            if isinstance(r, dict) and r.get("ticker") and r.get("cik_str") is not None}}
            cache["ticker_map"] = tmap
        for ticker in us_symbols(config, export_text):
            cik = (tmap.get("map") or {}).get(ticker)
            if not cik:
                continue
            try:
                subs = json.loads(lim.get(str(s["submissions_url"]).format(cik=cik)))
                recent = ((subs.get("filings") or {}).get("recent") or {})
                taken = 0
                for form, acc, filed, doc in zip(recent.get("form", []), recent.get("accessionNumber", []),
                                                 recent.get("filingDate", []), recent.get("primaryDocument", [])):
                    if form not in forms or filed < since:
                        continue
                    if per_symbol and taken >= per_symbol:
                        break
                    taken += 1
                    if acc in cache["filings"]:
                        continue
                    url = str(s["filing_url"]).format(cik=int(cik), accession=acc.replace("-", ""),
                                                      document=_raw_document(doc))
                    rows = parse_form4(lim.get(url), config)
                    for r in rows:
                        r["ticker"] = r["ticker"] or ticker
                        r["url"] = url
                    cache["filings"][acc] = {"ticker": ticker, "filed": filed, "rows": rows}
            except _Budget:
                raise
            except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"{ticker}: {exc}")
    except _Budget:
        errors.append(words.get("budget", ""))
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(str(exc))
    keep = (now - timedelta(days=int(s.get("keep_days") or s.get("lookback_days") or 0))).strftime("%Y-%m-%d")
    cache["filings"] = {k: v for k, v in cache["filings"].items() if str(v.get("filed") or "") >= keep}
    cache["checked_at"] = now.replace(microsecond=0).isoformat() + "Z"
    cache["message"] = (words.get("errors", "{n}").format(n=len(errors)) if errors else None)
    cache["errors"] = errors
    save_cache(s, cache)
    return cache


# -- views --------------------------------------------------------------------------
def rows(config: dict[str, Any], days: int | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Cached purchases at or over min_value_usd, newest first; `days` limits to recent ones."""
    s = settings(config)
    cache = load_cache(s)
    floor = float(s.get("min_value_usd") or 0)
    since = ((now or _today()) - timedelta(days=days)).strftime("%Y-%m-%d") if days else ""
    out = []
    for filing in cache["filings"].values():
        for r in filing.get("rows") or []:
            if (r.get("value") or 0) >= floor and str(r.get("date") or "") >= since:
                out.append(r)
    return sorted(out, key=lambda r: (str(r.get("date") or ""), r.get("value") or 0), reverse=True)


def panel(config: dict[str, Any]) -> dict[str, Any]:
    s = settings(config)
    words = s.get("words") or {}
    cache = load_cache(s)
    message = cache.get("message")
    if not s.get("enabled", True):
        message = words.get("disabled", "")
    elif not user_agent(config):
        message = words.get("no_user_agent", "")
    return {"heading": words.get("heading", ""), "message": message, "checked_at": cache.get("checked_at"),
            "rows": rows(config)[: int(s.get("panel_max") or 0) or None]}


def _money(v: Any) -> str:
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return "?"


def weekly_line(config: dict[str, Any], now: datetime | None = None) -> str:
    s = settings(config)
    words = s.get("words") or {}
    if not user_agent(config):
        return words.get("weekly_off", "")
    found = rows(config, days=int(s.get("weekly_days") or 7), now=now)
    if not found:
        return words.get("weekly_none", "")
    parts = [words.get("weekly_item", "{ticker}").format(ticker=r.get("ticker"), who=r.get("who"),
                                                          role=r.get("role"), value=_money(r.get("value")))
             for r in found[: int(s.get("weekly_max") or 0) or None]]
    return words.get("weekly_line", "{items}").format(n=len(found), items="; ".join(parts))
