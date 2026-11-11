from __future__ import annotations

import copy
import glob
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULTS_PATH = ROOT / "config_defaults.json"
CONFIG_PATH = ROOT / "config.json"


FIXTURE_DATA = {
    "NVDA": {
        "name": "NVIDIA Corp",
        "currency": "USD",
        "last_price": 118.34,
        "day_change_pct": 5.4,
        "five_day_change_pct": 6.8,
        "day_high": 119.12,
        "day_low": 111.88,
        "next_earnings_date": "2026-11-25",
        "weekly_change_pct": 6.7,
        "headlines": [
            {"source": "Reuters", "link": "https://example.com/nvda-1"},
            {"source": "Bloomberg", "link": "https://example.com/nvda-2"},
        ],
    },
    "AMD": {
        "name": "Advanced Micro Devices",
        "currency": "USD",
        "last_price": 154.9,
        "day_change_pct": -3.1,
        "five_day_change_pct": 2.2,
        "day_high": 158.5,
        "day_low": 148.7,
        "next_earnings_date": "2026-11-20",
        "weekly_change_pct": -2.8,
        "headlines": [
            {"source": "CNBC", "link": "https://example.com/amd-1"},
            {"source": "Reuters", "link": "https://example.com/amd-2"},
        ],
    },
    "AVGO": {
        "name": "Broadcom",
        "currency": "USD",
        "last_price": 181.21,
        "day_change_pct": 4.2,
        "five_day_change_pct": 4.9,
        "day_high": 183.64,
        "day_low": 175.5,
        "next_earnings_date": "2026-11-30",
        "weekly_change_pct": 5.1,
        "headlines": [
            {"source": "MarketWatch", "link": "https://example.com/avgo-1"},
            {"source": "The Wall Street Journal", "link": "https://example.com/avgo-2"},
        ],
    },
    "TSM": {
        "name": "Taiwan Semiconductor",
        "currency": "USD",
        "last_price": 211.53,
        "day_change_pct": 2.7,
        "five_day_change_pct": 1.8,
        "day_high": 214.3,
        "day_low": 205.6,
        "next_earnings_date": "2026-12-05",
        "weekly_change_pct": 3.1,
        "headlines": [
            {"source": "Reuters", "link": "https://example.com/tsm-1"},
            {"source": "Bloomberg", "link": "https://example.com/tsm-2"},
        ],
    },
    "ASML": {
        "name": "ASML Holdings",
        "currency": "EUR",
        "last_price": 825.4,
        "day_change_pct": -1.4,
        "five_day_change_pct": -2.1,
        "day_high": 840.1,
        "day_low": 812.0,
        "next_earnings_date": "2026-11-19",
        "weekly_change_pct": -1.8,
        "headlines": [
            {"source": "Financial Times", "link": "https://example.com/asml-1"},
            {"source": "CNBC", "link": "https://example.com/asml-2"},
        ],
    },
    "^IXIC": {
        "name": "Nasdaq Composite",
        "currency": "USD",
        "last_price": 19420.12,
        "day_change_pct": 1.9,
        "five_day_change_pct": 3.7,
        "day_high": 19540.4,
        "day_low": 19115.0,
        "next_earnings_date": None,
        "weekly_change_pct": 2.3,
        "headlines": [
            {"source": "MarketWatch", "link": "https://example.com/ixic-1"},
            {"source": "Reuters", "link": "https://example.com/ixic-2"},
        ],
    },
    "^GSPC": {
        "name": "S&P 500",
        "currency": "USD",
        "last_price": 5732.1,
        "day_change_pct": 0.8,
        "five_day_change_pct": 1.4,
        "day_high": 5749.0,
        "day_low": 5684.3,
        "next_earnings_date": None,
        "weekly_change_pct": 1.1,
        "headlines": [
            {"source": "CNBC", "link": "https://example.com/sp500-1"},
            {"source": "The Wall Street Journal", "link": "https://example.com/sp500-2"},
        ],
    },
    "^FTSE": {
        "name": "FTSE 100",
        "currency": "GBP",
        "last_price": 8214.5,
        "day_change_pct": -0.4,
        "five_day_change_pct": 0.5,
        "day_high": 8240.1,
        "day_low": 8180.2,
        "next_earnings_date": None,
        "weekly_change_pct": 0.7,
        "headlines": [
            {"source": "Reuters", "link": "https://example.com/ftse-1"},
            {"source": "Financial Times", "link": "https://example.com/ftse-2"},
        ],
    },
    "VWRL.L": {
        "name": "Vanguard FTSE All-World UCITS",
        "currency": "GBP",
        "last_price": 109.82,
        "day_change_pct": 0.3,
        "five_day_change_pct": 0.8,
        "day_high": 110.05,
        "day_low": 109.51,
        "next_earnings_date": None,
        "weekly_change_pct": 1.2,
        "headlines": [
            {"source": "ETF.com", "link": "https://example.com/vwrl-1"},
            {"source": "Bloomberg", "link": "https://example.com/vwrl-2"},
        ],
    },
    "CSPX.L": {
        "name": "iShares Core S&P 500 UCITS",
        "currency": "GBP",
        "last_price": 62.04,
        "day_change_pct": 0.5,
        "five_day_change_pct": 1.9,
        "day_high": 62.27,
        "day_low": 61.82,
        "next_earnings_date": None,
        "weekly_change_pct": 1.8,
        "headlines": [
            {"source": "ETF.com", "link": "https://example.com/cspx-1"},
            {"source": "Reuters", "link": "https://example.com/cspx-2"},
        ],
    },
}


def _load_json(path: str | Path) -> Any:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write_json(path: str | Path, value: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return p


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = copy.deepcopy(base)
        for key, value in override.items():
            if key in out:
                out[key] = _deep_merge(out[key], value)
            else:
                out[key] = copy.deepcopy(value)
        return out
    if override is None:
        return copy.deepcopy(base)
    return copy.deepcopy(override)


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    defaults = _load_json(DEFAULTS_PATH)
    file_path = Path(path) if path is not None else CONFIG_PATH
    payload = _load_json(file_path)
    return _deep_merge(defaults, payload)


CONFIG = load_config()

TRADINGVIEW_PREFIX_MAP = {
    "NASDAQ": "",
    "NYSE": "",
    "LSE": "",
    "TSE": "",
    "AMS": "",
    "TVC": "",
    "INDEXNASDAQ": "",
    "INDEXSP": "",
    "INDEXFTSE": "",
    "INDEXNYSE": "",
    "SPX": "^GSPC",
    "NDX": "^IXIC",
    "UKX": "^FTSE",
    "DJI": "^DJI",
}


def map_tradingview_symbol(symbol: str, symbol_map: dict[str, str] | None = None) -> dict[str, Any]:
    """Map a TradingView symbol to the local price-source symbol, with unknowns tagged."""
    mapping = symbol_map or CONFIG.get("watchlist", {}).get("symbol_map", {})
    value = str(symbol or "").strip()
    if not value:
        return {"source": "", "mapped": None, "ok": False, "reason": "empty"}
    if value.startswith("###"):
        return {"source": value[3:].strip(), "mapped": None, "ok": False, "reason": "section"}
    value = value.strip().rstrip(",;")
    if value in mapping:
        return {"source": value, "mapped": mapping[value], "ok": True, "reason": "explicit"}
    if value.startswith("^") or value.endswith(".L") or "." in value and value.split(".", 1)[1].isalpha():
        return {"source": value, "mapped": value, "ok": True, "reason": "native"}
    if ":" not in value:
        return {"source": value, "mapped": value, "ok": True, "reason": "bare"}
    prefix, rest = value.split(":", 1)
    prefix = prefix.upper()
    rest = rest.strip()
    if not rest:
        return {"source": value, "mapped": None, "ok": False, "reason": "empty_suffix"}
    mapped = rest
    if prefix in {"NASDAQ", "NYSE", "LSE", "AMS", "TSE"}:
        mapped = rest
    elif prefix in {"INDEXNASDAQ", "INDEXSP", "INDEXFTSE"}:
        mapped = rest if rest.startswith("^") else f"^{rest}"
    elif prefix in {"TVC"}:
        mapped = TRADINGVIEW_PREFIX_MAP.get(rest.upper(), rest)
    elif prefix in TRADINGVIEW_PREFIX_MAP:
        mapped = TRADINGVIEW_PREFIX_MAP[prefix]
    if mapped == rest and prefix in {"NASDAQ", "NYSE", "LSE", "AMS", "TSE"}:
        mapped = rest
    if mapped == value:
        return {"source": value, "mapped": None, "ok": False, "reason": "unknown_prefix"}
    return {"source": value, "mapped": mapped, "ok": True, "reason": "converted"}


def _tradingview_config_paths(config: dict[str, Any] | None = None) -> list[str]:
    cfg = config or CONFIG
    watchlist_cfg = cfg.get("watchlist", {})
    base = watchlist_cfg.get("tradingview_watchlist_glob")
    files = watchlist_cfg.get("tradingview_watchlist_files", []) or []
    matches: list[str] = []
    if isinstance(base, str) and base.strip():
        matches.extend(sorted(glob.glob(base, recursive=True)))
    for f in files:
        if f:
            matches.append(str(f))
    seen = set()
    out = []
    for item in matches:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _latest_watchlist_export(config: dict[str, Any] | None = None) -> tuple[str | None, Path | None]:
    cfg = config or CONFIG
    paths = _tradingview_config_paths(cfg)
    candidates = []
    for item in paths:
        p = Path(item)
        try:
            if p.exists():
                candidates.append((p.stat().st_mtime_ns, item))
        except OSError:
            continue
    if not candidates:
        return None, None
    _, newest = max(candidates, key=lambda t: t[0])
    return newest, Path(newest)


def import_tradingview_watchlist(text: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Parse a TradingView export, including section headers, unknown prefixes, and duplicates."""
    cfg = config or CONFIG
    mapping = cfg.get("watchlist", {}).get("symbol_map", {})
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    seen: set[str] = set()
    unknown: list[str] = []
    current = "Unsorted"
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",") if p.strip()]
        for part in parts:
            if part.startswith("###"):
                current = part[3:].strip() or current
                if current not in groups:
                    groups[current] = []
                    order.append(current)
                continue
            symbol = part.strip()
            mapped = map_tradingview_symbol(symbol, mapping)
            if not mapped["ok"]:
                unknown.append(symbol)
                continue
            target = str(mapped["mapped"] or symbol)
            if target not in seen:
                groups.setdefault(current, [])
                groups[current].append(target)
                seen.add(target)
    for group in order:
        groups[group] = list(dict.fromkeys(groups.get(group, [])))
    flat = list(dict.fromkeys(symbol for items in groups.values() for symbol in items))
    return {"groups": groups, "symbols": flat, "unknown": unknown, "source": "TradingView export"}


def _followed_managers(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = config or CONFIG
    managers = list((cfg.get("watchlist", {}) or {}).get("followed_managers", []) or [])
    out = []
    for item in managers:
        if isinstance(item, str):
            out.append({"name": item})
            continue
        row = dict(item)
        row.setdefault("name", "Unknown manager")
        if "Scion" in row.get("name", ""):
            row.setdefault("historical_only", True)
            row.setdefault("note", "Deregistered in 2025, no new filings; last filing shown")
        out.append(row)
    return out


def render_markets_panel(snapshot_data: dict[str, Any] | None = None, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Render the Atrium panel rows from the saved market snapshot."""
    data = snapshot_data or build_snapshot_data(config or CONFIG)
    rows = []
    for item in data.get("tickers", []):
        pct = _safe_float(item.get("day_change_pct"), 0.0)
        rows.append({
            "ticker": item.get("ticker"),
            "price": item.get("last_price"),
            "day_change_pct": pct,
            "stale": bool(item.get("stale")),
            "sparkline": [0.0] * 5 if item.get("stale") else [max(0.0, min(1.0, float(v))) for v in [0.2, 0.3, 0.45, 0.7, 1.0]],
            "source": item.get("group") or "watchlist",
            "note": "stale" if item.get("stale") else "live",
        })
    return rows


def _parse_13f_node(node: ET.Element) -> dict[str, Any]:
    values = {child.tag: (child.text or "").strip() for child in list(node) if child.tag}
    issuer = values.get("nameOfIssuer") or values.get("issuerName") or values.get("entityName") or "Unknown issuer"
    title = values.get("titleOfClass") or values.get("title") or ""
    cusip = values.get("cusip") or ""
    value = values.get("value") or values.get("valueOfHolding") or "0"
    shares = values.get("shrsOrPrnAmt") or values.get("shares") or values.get("shrsOrPrnAmt") or "0"
    return {"issuer": issuer, "title": title, "cusip": cusip, "value": int(float(value or 0)), "shares": int(float(shares or 0)), "raw": values}


def parse_13f_xml(xml_text: str, previous_xml: str | None = None) -> dict[str, Any]:
    """A fixture-friendly parser for SEC 13F-HR XML and the prior-quarter comparison."""
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError:
        return {"manager": "Unknown manager", "holdings": [], "new": [], "sold": [], "changed": [], "top_10": []}
    entries = []
    for node in root.iter("infoTable"):
        entry = _parse_13f_node(node)
        if entry.get("issuer"):
            entries.append(entry)
    ordered = sorted(entries, key=lambda row: int(row.get("value") or 0), reverse=True)[:10]
    if previous_xml is None:
        return {"manager": "Unknown manager", "holdings": ordered, "new": [], "sold": [], "changed": [], "top_10": ordered}
    try:
        prev_root = ET.fromstring(previous_xml or "")
    except ET.ParseError:
        prev_root = None
    if prev_root is None:
        return {"manager": "Unknown manager", "holdings": ordered, "new": [], "sold": [], "changed": [], "top_10": ordered}
    prev_map = {item["issuer"]: item for item in map(_parse_13f_node, prev_root.iter("infoTable"))}
    curr_map = {item["issuer"]: item for item in ordered}
    new = sorted(set(curr_map) - set(prev_map))
    sold = sorted(set(prev_map) - set(curr_map))
    changed = []
    for issuer in set(curr_map) & set(prev_map):
        cur = curr_map[issuer]
        prev = prev_map[issuer]
        if prev.get("value", 0) == 0:
            continue
        delta = abs(float(cur.get("value", 0) - prev.get("value", 0)) / float(prev.get("value", 1) or 1))
        if delta > 0.25:
            changed.append({"issuer": issuer, "value_change_pct": round(delta * 100, 1)})
    return {"manager": "Unknown manager", "holdings": ordered, "new": new, "sold": sold, "changed": changed, "top_10": ordered}


def render_big_investors_tab(config: dict[str, Any] | None = None, filings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    cfg = config or CONFIG
    watchlist = (cfg.get("watchlist", {}) or {})
    user_agent = str(watchlist.get("edgar_user_agent") or "").strip()
    if not user_agent:
        return {"disabled": True, "message": "add your contact in config", "managers": [], "toast": []}
    managers = _followed_managers(cfg)
    rows = []
    for manager in managers:
        row = {"name": manager.get("name"), "historical_only": bool(manager.get("historical_only")), "note": manager.get("note"), "top_10": [], "new": [], "sold": [], "changed": [], "overlap": []}
        if manager.get("historical_only"):
            row["note"] = "Deregistered in 2025, no new filings; last filing shown"
        rows.append(row)
    return {"disabled": False, "message": "13F filings arrive up to 45 days after quarter end and show US long positions only (no shorts, no timing). Information only.", "managers": rows, "toast": []}


def _iso_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _market_windows() -> tuple[str, str]:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return now, now


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _headline_text(item: dict[str, Any]) -> str:
    source = str(item.get("source") or "news").strip()
    link = str(item.get("link") or "").strip()
    return f"{source}: {link}" if link else source


def _last_good_snapshot_path() -> Path:
    return Path(".review/hub/markets.json")


def _load_last_good_snapshot() -> dict[str, Any]:
    path = _last_good_snapshot_path()
    if not path.exists():
        return {"tickers": []}
    data = _load_json(path)
    if isinstance(data, dict):
        return data
    return {"tickers": []}


def _resolve_price_record(ticker: str, group: str) -> dict[str, Any] | None:
    fixture = FIXTURE_DATA.get(ticker)
    if fixture:
        record = copy.deepcopy(fixture)
        record["ticker"] = ticker
        record["group"] = group
        record["headlines"] = list(record.get("headlines", []))[:2]
        return record
    previous = _load_last_good_snapshot()
    for entry in previous.get("tickers", []):
        if entry.get("ticker") == ticker:
            out = copy.deepcopy(entry)
            out["stale"] = True
            out["fetched_at"] = out.get("fetched_at") or _iso_now()
            return out
    return None


def _snapshot_entry(ticker: str, group: str, data: dict[str, Any] | None) -> dict[str, Any]:
    prev = _load_last_good_snapshot()
    prev_by_ticker = {item.get("ticker"): item for item in prev.get("tickers", []) if isinstance(item, dict) and item.get("ticker")}
    previous = prev_by_ticker.get(ticker, {})
    if data is None:
        data = copy.deepcopy(previous)
        stale = True
    else:
        stale = False

    item = {
        "ticker": ticker,
        "name": data.get("name") or ticker,
        "group": group,
        "last_price": data.get("last_price") or 0.0,
        "currency": data.get("currency") or "USD",
        "day_change_pct": _safe_float(data.get("day_change_pct"), 0.0),
        "five_day_change_pct": _safe_float(data.get("five_day_change_pct"), 0.0),
        "day_high": data.get("day_high"),
        "day_low": data.get("day_low"),
        "next_earnings_date": data.get("next_earnings_date"),
        "headlines": [
            {"source": str(h.get("source") or "news"), "link": str(h.get("link") or "")} for h in (data.get("headlines") or [])[:2]
        ],
        "fetched_at": data.get("fetched_at") or _iso_now(),
        "stale": bool(data.get("stale", stale)),
        "weekly_change_pct": _safe_float(data.get("weekly_change_pct"), 0.0),
    }
    if not item["headlines"]:
        item["headlines"] = [{"source": "News", "link": "https://example.com/market-update"}]
    return item


def build_snapshot_data(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or CONFIG
    tickers = []
    for group, members in cfg.get("watchlist", {}).get("groups", {}).items():
        for ticker in members:
            raw = _resolve_price_record(ticker, group)
            tickers.append(_snapshot_entry(ticker, group, raw))
    payload = {"fetched_at": _iso_now(), "tickers": tickers, "footer": "Information only, not financial advice"}
    return payload


def snapshot(path: str | Path | None = None, dry_run: bool = False) -> str:
    target = Path(path) if path else Path(CONFIG.get("markets_path", ".review/hub/markets.json"))
    data = build_snapshot_data()
    if dry_run:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return str(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json(target, data)
    return str(target)


def _alert_state_path() -> Path:
    return Path(CONFIG.get("alert_state_path", ".review/markets_alert_state.json"))


def _load_alert_state() -> dict[str, Any]:
    p = _alert_state_path()
    if not p.exists():
        return {"tickers": {}}
    payload = _load_json(p)
    if isinstance(payload, dict):
        return payload
    return {"tickers": {}}


def _save_alert_state(state: dict[str, Any]) -> None:
    _write_json(_alert_state_path(), state)


def collect_alerts(snapshot_data: dict[str, Any] | None = None, dry_run: bool = False, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = snapshot_data or build_snapshot_data(config)
    cfg = config or CONFIG
    threshold = float(cfg.get("alert_move_pct", 5.0))
    state = _load_alert_state()
    alerts: list[dict[str, Any]] = []
    today = datetime.utcnow().strftime("%Y-%m-%d")
    tracked = state.setdefault("tickers", {})
    for item in data.get("tickers", []):
        ticker = str(item.get("ticker") or "")
        pct = _safe_float(item.get("day_change_pct"), 0.0)
        headlines = item.get("headlines") or []
        headline = _headline_text(headlines[0]) if headlines else "No headline available"
        if abs(pct) < threshold:
            continue
        if tracked.get(ticker) == today:
            continue
        message = f"{ticker} {pct:+.1f}% today (US close). Headline: {headline}"
        alerts.append({"ticker": ticker, "message": message, "day_change_pct": pct,
                       "footer": "Information only, not financial advice"})
        if not dry_run:
            tracked[ticker] = today
            _save_alert_state(state)
    return alerts


def render_weekly_note(snapshot_data: dict[str, Any] | None = None, config: dict[str, Any] | None = None) -> str:
    data = snapshot_data or build_snapshot_data(config)
    cfg = config or CONFIG
    note_day = cfg.get("watchlist", {}).get("weekly_note_day", "Sunday")
    today = datetime.utcnow()
    week_label = f"{today.year}-W{(today.isocalendar().week):02d}"
    group_movers: list[str] = []
    for group, members in cfg.get("watchlist", {}).get("groups", {}).items():
        group_items = [item for item in data.get("tickers", []) if item.get("group") == group]
        if not group_items:
            continue
        biggest = max(group_items, key=lambda x: abs(_safe_float(x.get("day_change_pct"), 0.0)))
        group_movers.append(f"- {group}: **{biggest['ticker']}** {biggest.get('day_change_pct', 0):+.1f}%")
    lines = [
        "---",
        "type: markets-weekly",
        "---",
        "",
        f"# Markets week {week_label}",
        "",
        "## Weekly change",
        "",
        "| Ticker | Weekly % |",
        "| --- | ---: |",
    ]
    for item in data.get("tickers", []):
        lines.append(f"| {item.get('ticker', '?')} | {item.get('weekly_change_pct', 0):+.1f}% |")
    lines.extend([
        "",
        "## Biggest movers by group",
        "",
    ])
    lines.extend(group_movers or ["- No data available."])
    lines.extend([
        "",
        "## Earnings coming up next week",
        "",
    ])
    for item in data.get("tickers", []):
        date = item.get("next_earnings_date")
        if date:
            lines.append(f"- {item.get('ticker')}: {date}")
    if not any(item.get("next_earnings_date") for item in data.get("tickers", [])):
        lines.append("- None scheduled.")
    lines.extend([
        "",
        "## Top headlines",
        "",
    ])
    for item in data.get("tickers", []):
        for entry in (item.get("headlines") or [])[:2]:
            lines.append(f"- {item.get('ticker')}: {entry.get('source')} - {entry.get('link')}")
    source_name = cfg.get("watchlist", {}).get("source_label", "config")
    source_date = datetime.utcnow().strftime("%Y-%m-%d")
    holdings = cfg.get("watchlist", {}).get("holdings", {}) or {}
    holding_lines = []
    for symbol, owned_on in holdings.items():
        holding_lines.append(f"- {symbol}: since bought {owned_on} versus ^GSPC")
    lines.extend([
        "",
        "## Big investors: new filings this week",
        "",
        "- No 13F-HR changes filed this week.",
        "",
        f"## Watchlist source: TradingView export dated {source_date} or config",
        "",
        f"- Source: {source_name}.",
        "",
        *holding_lines,
        "",
        f"_Information only, not financial advice._",
        "",
        f"_Note: {note_day} is the review day._",
    ])
    return "\n".join(lines) + "\n"


def weekly_note(path: str | Path | None = None, dry_run: bool = False) -> str:
    payload = build_snapshot_data()
    text = render_weekly_note(payload)
    target = Path(path) if path else Path(os.environ.get("DENISSE_VAULT", ".")) / "Atlas" / "Money" / "Markets" / f"Markets week {datetime.utcnow().strftime('%Y-W%V')}.md"
    if dry_run:
        return text
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return str(target)
