"""Who's trading what: big funds' quarter-on-quarter 13F-HR moves (SEC EDGAR).

For every fund in the markets config `funds.list` (name, manager, SEC CIK) the two newest
13F-HR filings are read (the submissions JSON, the filing's index, its information-table
XML), diffed (new positions, exits, adds and trims over the config thresholds) and summed up
with the largest positions and their share of the portfolio.

Every value (URLs, forms, thresholds, name matching, cache and hub paths, wording) comes from
the markets config `funds` block, documented in markets/README.md; the request settings that
are not set there (user agent, rate limit, budget, timeout, ticker map, submissions URL) are
the insiders block's, so both share one SEC fair-access policy. Information only: this lists
what was filed, it does not judge it.

`refresh()` is the only function that touches the network, and only when
`watchlist.edgar_user_agent` is set. Everything else reads the cache under .review/. Tests
pass their own `fetch`, `sleep` and `clock`.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import insiders as _ins

Fetch = _ins.Fetch
_SHARED = ("min_request_interval_s", "max_requests_per_run", "timeout_s", "ticker_map_url",
           "ticker_map_max_age_days", "submissions_url")


# -- config -------------------------------------------------------------------
def settings(config: dict[str, Any]) -> dict[str, Any]:
    """The funds block, with the request settings it does not set taken from insiders."""
    ins = _ins.settings(config)
    s = {k: ins[k] for k in _SHARED if k in ins}
    s.update((config or {}).get("funds") or {})
    return s


def words(config: dict[str, Any]) -> dict[str, Any]:
    return dict(settings(config).get("words") or {})


def fund_list(config: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for f in settings(config).get("list") or []:
        if isinstance(f, dict) and str(f.get("cik") or "").strip():
            row = dict(f)
            row["cik"] = str(row["cik"]).strip().zfill(10)
            row.setdefault("name", row["cik"])
            out.append(row)
    return out


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
    data.setdefault("funds", {})
    data.setdefault("names", {})
    return data


def save_cache(s: dict[str, Any], data: dict[str, Any]) -> Path:
    p = _cache_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


# -- the information table -----------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element, name: str) -> ET.Element | None:
    for c in node:
        if _local(c.tag) == name:
            return c
    return None


def _ctext(node: ET.Element | None, *path: str) -> str:
    for name in path:
        if node is None:
            return ""
        node = _child(node, name)
    return ((node.text if node is not None else "") or "").strip()


def _number(v: str) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def parse_info_table(xml_text: str | bytes, config: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Positions in one 13F information table, summed per CUSIP and put/call (a fund reports
    one line per manager/discretion). None when this is not an information table."""
    s = settings(config)
    mult = float(s.get("value_multiplier") or 1)
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, TypeError, ValueError):
        return None
    rows = [n for n in root.iter() if _local(n.tag) == str(s.get("info_row_tag") or "infoTable")]
    if not rows:
        return None
    merged: dict[str, dict[str, Any]] = {}
    for n in rows:
        cusip = _ctext(n, "cusip").upper()
        issuer = _ctext(n, "nameOfIssuer")
        put_call = _ctext(n, "putCall").lower()
        key = cusip or issuer.upper()
        if put_call:
            key += "/" + put_call
        row = merged.setdefault(key, {"key": key, "cusip": cusip, "issuer": issuer,
                                      "title": _ctext(n, "titleOfClass"), "put_call": put_call,
                                      "shares": 0.0, "value": 0.0})
        row["shares"] += _number(_ctext(n, "shrsOrPrnAmt", "sshPrnamt"))
        row["value"] += _number(_ctext(n, "value")) * mult
    return sorted(merged.values(), key=lambda r: r["value"], reverse=True)


# -- issuer -> ticker --------------------------------------------------------------
def normalise_name(name: str, s: dict[str, Any]) -> str:
    """Upper case, punctuation to spaces, abbreviations spelt out, filler words dropped, so a
    13F issuer name and a company_tickers.json title can be compared."""
    abbrev = {str(k).upper(): str(v).upper() for k, v in (s.get("name_abbreviations") or {}).items()}
    drop = {str(w).upper() for w in s.get("name_drop_words") or []}
    out = []
    for w in re.sub(r"[^A-Z0-9]+", " ", str(name or "").upper()).split():
        w = abbrev.get(w, w)
        out += [x for x in w.split() if x not in drop]
    return " ".join(out)


def name_map(ticker_rows: Any, s: dict[str, Any]) -> dict[str, list[str]]:
    rows = ticker_rows.values() if isinstance(ticker_rows, dict) else ticker_rows or []
    out: dict[str, list[str]] = {}
    for r in rows:
        if isinstance(r, dict) and r.get("ticker") and r.get("title"):
            key = normalise_name(r["title"], s)
            if key:
                out.setdefault(key, [])
                if str(r["ticker"]).upper() not in out[key]:
                    out[key].append(str(r["ticker"]).upper())
    return out


def tickers_for(row: dict[str, Any], names: dict[str, list[str]], s: dict[str, Any]) -> list[str]:
    """Ticker candidates for a position: the config CUSIP map first, else every ticker whose
    company title matches the issuer name. One candidate = a ticker; more = ambiguous (share
    classes), the issuer name is shown."""
    fixed = (s.get("cusip_tickers") or {}).get(str(row.get("cusip") or "").upper())
    if fixed:
        return [str(fixed).upper()]
    return list(names.get(normalise_name(row.get("issuer", ""), s), []))


# -- the diff -----------------------------------------------------------------------
def diff(previous: list[dict[str, Any]] | None, current: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """New positions, exits, and adds/trims whose share count moved by at least move_min_pct
    and whose value (shares moved x this quarter's price) by at least move_min_usd. Biggest
    money first. No previous filing = no moves (nothing to compare with)."""
    s = settings(config)
    if previous is None:
        return []
    min_pct = float(s.get("move_min_pct") or 0)
    min_usd = float(s.get("move_min_usd") or 0)
    min_new = float(s.get("new_exit_min_usd") or 0)
    total = sum(r["value"] for r in current) or 0.0
    prev = {r["key"]: r for r in previous}
    cur = {r["key"]: r for r in current}
    moves = []
    for key, c in cur.items():
        p = prev.get(key)
        share = round(100 * c["value"] / total, 2) if total else None
        base = {"key": key, "cusip": c["cusip"], "issuer": c["issuer"], "put_call": c["put_call"],
                "shares_before": p["shares"] if p else 0.0, "shares_after": c["shares"],
                "value_before": p["value"] if p else 0.0, "value_after": c["value"], "pct_of_portfolio": share}
        if p is None or p["shares"] <= 0:
            if c["value"] >= min_new:
                moves.append(dict(base, kind="new", change_pct=None, value_change=c["value"]))
            continue
        pct = 100 * (c["shares"] - p["shares"]) / p["shares"]
        price = c["value"] / c["shares"] if c["shares"] else 0.0
        money = (c["shares"] - p["shares"]) * price
        if abs(pct) >= min_pct and abs(money) >= min_usd and pct != 0:
            moves.append(dict(base, kind="add" if pct > 0 else "trim", change_pct=round(pct, 1), value_change=money))
    for key, p in prev.items():
        if key not in cur and p["value"] >= min_new:
            moves.append({"key": key, "cusip": p["cusip"], "issuer": p["issuer"], "put_call": p["put_call"],
                          "shares_before": p["shares"], "shares_after": 0.0, "value_before": p["value"],
                          "value_after": 0.0, "pct_of_portfolio": 0.0, "kind": "exit", "change_pct": -100.0,
                          "value_change": -p["value"]})
    return sorted(moves, key=lambda m: abs(m["value_change"]), reverse=True)


def quarter(period: str, config: dict[str, Any]) -> str:
    """'2026-09-30' -> the config quarter label (e.g. 'Q3 2026'); unknown dates as given."""
    try:
        d = datetime.strptime(str(period)[:10], "%Y-%m-%d")
    except ValueError:
        return str(period or "")
    return str(words(config).get("quarter", "{year}-{month:02d}")).format(q=(d.month - 1) // 3 + 1,
                                                                         year=d.year, month=d.month)


# -- EDGAR --------------------------------------------------------------------------
def _pick_filings(subs: dict[str, Any], s: dict[str, Any]) -> list[dict[str, str]]:
    """The newest filing per report period, newest periods first, filings_per_fund of them."""
    forms = {str(f) for f in s.get("forms") or []}
    recent = (subs.get("filings") or {}).get("recent") or {}
    cols = [recent.get(k) or [] for k in ("form", "accessionNumber", "filingDate", "reportDate", "primaryDocument")]
    found = [dict(zip(("form", "accession", "filed", "period", "primary"), row)) for row in zip(*cols)]
    found = sorted((f for f in found if f["form"] in forms), key=lambda f: (f["filed"], f["accession"]), reverse=True)
    out, seen = [], set()
    for f in found:
        if f["period"] in seen:
            continue
        seen.add(f["period"])
        out.append(f)
        if len(out) >= int(s.get("filings_per_fund") or 2):
            break
    return out


def _info_table(lim, cik: str, f: dict[str, str], config: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    s = settings(config)
    folder = dict(cik=int(cik), accession=f["accession"].replace("-", ""))
    index = json.loads(lim.get(str(s["index_url"]).format(**folder)))
    skip = {str(x).lower() for x in s.get("skip_documents") or []}
    skip.add(str(f.get("primary") or "").rsplit("/", 1)[-1].lower())
    ext = str(s.get("info_table_ext") or ".xml").lower()
    names = [str(i.get("name") or "") for i in ((index.get("directory") or {}).get("item") or [])
             if isinstance(i, dict)]
    for name in names:
        if not name.lower().endswith(ext) or name.lower() in skip:
            continue
        rows = parse_info_table(lim.get(str(s["filing_url"]).format(document=name, **folder)), config)
        if rows is not None:
            return rows, name
    raise ValueError(words(config).get("no_table", "").format(accession=f["accession"]))


def _due(cache: dict[str, Any], s: dict[str, Any], now: datetime) -> bool:
    hours = float(s.get("refresh_hours") or 0)
    last = str(cache.get("checked_at") or "").rstrip("Z")
    try:
        return not last or now - datetime.fromisoformat(last) >= timedelta(hours=hours)
    except ValueError:
        return True


def refresh(config: dict[str, Any], fetch: Fetch | None = None, sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic, now: datetime | None = None,
            force: bool = True) -> dict[str, Any]:
    """Fetch each fund's newest 13F-HR filings into the cache (each filing once). Without a user
    agent it does nothing and records why. force=False skips a refresh made less than
    refresh_hours ago (13F filings come quarterly; the snapshot runs every few minutes)."""
    s = settings(config)
    w = words(config)
    cache = load_cache(s)
    now = now or _ins._today()
    agent = _ins.user_agent(config)
    if not s.get("enabled", True) or not agent:
        cache["message"] = w.get("disabled" if not s.get("enabled", True) else "no_user_agent", "")
        save_cache(s, cache)
        return cache
    if not force and not _due(cache, s, now):
        return cache
    lim = _ins._Limiter(s, fetch or _ins.http_fetch(float(s.get("timeout_s") or 20)), agent, sleep, clock)
    lim.last = clock()  # a refresh may follow the insiders one: keep the gap from the first request too
    errors: list[str] = []
    try:
        names = cache.get("names") or {}
        age = int(s.get("ticker_map_max_age_days") or 0)
        if not names.get("map") or str(names.get("fetched") or "") < (now - timedelta(days=age)).strftime("%Y-%m-%d"):
            names = {"fetched": now.strftime("%Y-%m-%d"),
                     "map": name_map(json.loads(lim.get(str(s["ticker_map_url"]))), s)}
            cache["names"] = names
        for fund in fund_list(config):
            cik = fund["cik"]
            try:
                picked = _pick_filings(json.loads(lim.get(str(s["submissions_url"]).format(cik=cik))), s)
                for f in picked:
                    if f["accession"] in cache["filings"]:
                        continue
                    rows, doc = _info_table(lim, cik, f, config)
                    cache["filings"][f["accession"]] = {"cik": cik, "form": f["form"], "filed": f["filed"],
                                                        "period": f["period"], "document": doc, "rows": rows}
                cache["funds"][cik] = {"accessions": [f["accession"] for f in picked],
                                       "checked": now.strftime("%Y-%m-%d")}
            except _ins._Budget:
                raise
            except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f'{fund["name"]}: {exc}')
    except _ins._Budget:
        errors.append(w.get("budget", ""))
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(str(exc))
    wanted = {a for f in cache["funds"].values() for a in f.get("accessions") or []}
    cache["filings"] = {k: v for k, v in cache["filings"].items() if k in wanted}
    cache["checked_at"] = now.replace(microsecond=0).isoformat() + "Z"
    cache["message"] = w.get("errors", "{n}").format(n=len(errors)) if errors else None
    cache["errors"] = errors
    save_cache(s, cache)
    return cache


# -- views --------------------------------------------------------------------------
def _label(row: dict[str, Any], names: dict[str, list[str]], s: dict[str, Any], watch: set[str]) -> dict[str, Any]:
    found = tickers_for(row, names, s)
    out = dict(row)
    out["ticker"] = found[0] if len(found) == 1 else None
    out["on_watchlist"] = any(t in watch for t in found)
    return out


def summary(config: dict[str, Any], watchlist: list[str] | None = None) -> dict[str, Any]:
    """Per fund (config order): its newest filing and the one before (filed date, quarter
    covered), the moves between them, the largest positions with their share of the
    portfolio, and the moves in watchlist tickers. Read from the cache only."""
    s = settings(config)
    w = words(config)
    cache = load_cache(s)
    names = (cache.get("names") or {}).get("map") or {}
    has_filings = any((cache["funds"].get(f["cik"]) or {}).get("accessions") for f in fund_list(config))
    if watchlist is None:  # the watchlist read globs for the TradingView export: only when there is something to match
        watchlist = _ins.us_symbols(config) if has_filings else []
    watch = {str(t).upper() for t in watchlist}
    top_n = int(s.get("top_positions") or 0)
    funds, hits = [], []
    for fund in fund_list(config):
        accs = (cache["funds"].get(fund["cik"]) or {}).get("accessions") or []
        filings = [dict(cache["filings"][a], accession=a) for a in accs if a in cache["filings"]]
        row = {"name": fund["name"], "manager": fund.get("manager", ""), "cik": fund["cik"],
               "note": fund.get("note", ""), "latest": None, "previous": None, "moves": [], "top": [],
               "watch": [], "total_value": 0.0, "positions": 0}
        if filings:
            latest, prev = filings[0], (filings[1] if len(filings) > 1 else None)
            meta = lambda f: {"accession": f["accession"], "filed": f["filed"], "period": f["period"],
                              "quarter": quarter(f["period"], config), "form": f.get("form", "")}
            row["latest"] = meta(latest)
            row["previous"] = meta(prev) if prev else None
            total = sum(r["value"] for r in latest["rows"]) or 0.0
            row["total_value"], row["positions"] = total, len(latest["rows"])
            row["top"] = [dict(_label(r, names, s, watch), pct_of_portfolio=round(100 * r["value"] / total, 2) if total else None)
                          for r in latest["rows"][:top_n or None]]
            row["moves"] = [_label(m, names, s, watch) for m in diff(prev["rows"] if prev else None, latest["rows"], config)]
            row["watch"] = [m for m in row["moves"] if m["on_watchlist"]]
            for m in row["watch"]:
                hits.append(dict(m, fund=fund["name"], filed=latest["filed"], quarter=row["latest"]["quarter"]))
        funds.append(row)
    message = cache.get("message")
    if not s.get("enabled", True):
        message = w.get("disabled", "")
    elif not _ins.user_agent(config):
        message = w.get("no_user_agent", "")
    elif not cache.get("checked_at"):
        message = w.get("never", "")
    return {"heading": w.get("heading", ""), "small_print": w.get("small_print", ""), "message": message,
            "checked_at": cache.get("checked_at"), "funds": funds, "watch": hits}


def money(v: Any, config: dict[str, Any]) -> str:
    """$1.5B / $758M / $12K by the config units; under the smallest unit, whole dollars."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "?"
    w = words(config)
    sign = "-" if x < 0 else ""
    for size, unit in w.get("money_units") or []:
        if abs(x) >= float(size):
            return w.get("money", "{sign}${n}{unit}").format(sign=sign, n=f"{abs(x) / float(size):,.1f}".rstrip("0").rstrip("."), unit=unit)
    return w.get("money", "{sign}${n}{unit}").format(sign=sign, n=f"{abs(x):,.0f}", unit="")


def move_text(m: dict[str, Any], config: dict[str, Any]) -> str:
    """One move in the config wording (words.move, one template per kind)."""
    w = words(config)
    pc = (w.get("put_call") or {}).get(m.get("put_call") or "", "")
    template = (w.get("move") or {}).get(m.get("kind"), "{money}")
    return str(template).format(money=money(abs(m.get("value_change") or 0), config),
                                pct=abs(m["change_pct"]) if m.get("change_pct") is not None else 0,
                                share=m.get("pct_of_portfolio") or 0, put_call=pc)


def who(m: dict[str, Any]) -> str:
    return m.get("ticker") or m.get("issuer") or m.get("cusip") or "?"


def hub_payload(config: dict[str, Any]) -> dict[str, Any]:
    """What .review/hub/funds.json carries for the Atrium card: the summary with each move's
    text already worded (the card stays a renderer), plus the insiders panel."""
    s = settings(config)
    data = summary(config)
    n = int(s.get("card_moves") or 0)
    for f in data["funds"]:
        f["moves"] = [dict(m, text=move_text(m, config), label=who(m)) for m in f["moves"]]
        f["card_moves"] = f["moves"][:n or None]
        f["top"] = [dict(t, label=who(t)) for t in f["top"]]
    data["watch"] = [dict(m, text=move_text(m, config), label=who(m)) for m in data["watch"]]
    data["fund_line"] = words(config).get("fund_line", "")
    data["insiders"] = _ins.panel(config)
    return data


def write_hub(config: dict[str, Any], path: str | Path | None = None) -> Path:
    target = Path(path or settings(config).get("hub_path") or "")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(hub_payload(config), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return target


def text_summary(config: dict[str, Any]) -> str:
    """The `python -m markets funds` printout."""
    w = words(config)
    data = summary(config)
    lines = [w.get("heading", "")]
    if data["message"]:
        lines.append(data["message"])
    for f in data["funds"]:
        lines.append("")
        if not f["latest"]:
            lines.append(w.get("fund_none", "{name}").format(name=f["name"]))
            continue
        lines.append(w.get("fund_line", "{name}").format(name=f["name"], manager=f["manager"], **f["latest"],
                                                         total=money(f["total_value"], config), positions=f["positions"]))
        if f["note"]:
            lines.append("  " + f["note"])
        if not f["previous"]:
            lines.append("  " + w.get("no_previous", ""))
        for m in f["moves"][: int(settings(config).get("cli_moves") or 0) or None]:
            star = w.get("watch_mark", "") if m["on_watchlist"] else ""
            lines.append(f'  {star}{who(m)}: {move_text(m, config)}')
        if f["top"]:
            lines.append("  " + w.get("top_heading", ""))
            for t in f["top"]:
                lines.append("    " + w.get("top_line", "{label}").format(label=who(t), money=money(t["value"], config),
                                                                         share=t.get("pct_of_portfolio") or 0))
    lines += ["", w.get("small_print", "")]
    return "\n".join(lines) + "\n"


def weekly_section(config: dict[str, Any], now: datetime | None = None) -> list[str]:
    """Weekly note lines: one ### section per fund whose newest 13F-HR was filed in the last
    weekly_days, with its biggest moves; otherwise the none/off line."""
    s = settings(config)
    w = words(config)
    if not s.get("enabled", True) or not _ins.user_agent(config):
        return [w.get("weekly_off", "")]
    since = ((now or _ins._today()) - timedelta(days=int(s.get("weekly_days") or 7))).strftime("%Y-%m-%d")
    out: list[str] = []
    for f in summary(config)["funds"]:
        if not f["latest"] or f["latest"]["filed"] < since:
            continue
        out += ["", w.get("weekly_fund", "### {name}").format(name=f["name"], **f["latest"]), ""]
        moves = f["moves"][: int(s.get("weekly_moves") or 0) or None]
        out += [w.get("weekly_move", "- {label}: {text}").format(label=who(m), text=move_text(m, config)) for m in moves]
        if not moves:
            out.append(w.get("weekly_no_moves", ""))
    return out[1:] if out else [w.get("weekly_none", "")]
