"""Summaries for Orchestra task credits and ledger backfills."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REVIEW = ROOT / ".review"


def _config_path() -> Path:
    cfg = ROOT / "orchestra" / "config.json"
    if cfg.exists():
        return cfg
    return ROOT / "orchestra" / "config_defaults.json"


def load_config() -> dict:
    path = _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    defaults = {}
    dpath = ROOT / "orchestra" / "config_defaults.json"
    if dpath.exists():
        try:
            defaults = json.loads(dpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            defaults = {}
    out = {**defaults, **data}
    return out


def ledger_path(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    p = cfg.get("credits_ledger_file", "credits.jsonl")
    if str(p).startswith("/"):
        return Path(p)
    return REVIEW / str(p)


def read_ledger(path: Path | None = None) -> list[dict]:
    p = path or ledger_path()
    if not p.exists():
        return []
    entries: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            entries.append(data)
    return entries


def _parse_cost(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    m = re.search(r"(?:\$|~\$)([0-9]+(?:\.[0-9]+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _parse_tokens(text: str | None) -> dict[str, int | None]:
    if not text or "tokens" not in text or "n/a" in text.lower():
        return {"in": None, "out": None, "cache_read": None, "cache_write": None}
    m = re.search(r"tokens\s+in\s+(\d+)\s+out\s+(\d+)\s+(?:cache-read\s+(\d+)\s+)?(?:cache-write\s+(\d+)\s+)?",
                  text, re.I)
    if not m:
        return {"in": None, "out": None, "cache_read": None, "cache_write": None}
    return {"in": int(m.group(1)), "out": int(m.group(2)), "cache_read": int(m.group(3)) if m.group(3) else None,
            "cache_write": int(m.group(4)) if m.group(4) else None}


def _parse_runner_log_entry(line: str, last_lane: dict[str, str] | None = None) -> dict | None:
    line = line.strip()
    if not line:
        return None

    m = re.match(r"^\S+\s+START\s+(\S+)\s+.*?\blane=(\w+)", line)
    if m:
        if last_lane is not None:
            last_lane[m.group(1)] = m.group(2)
        return None

    m = re.match(r"^\S+\s+LIMITED\s+(\S+)\s+lane=(\w+)", line)
    if m:
        if last_lane is not None:
            last_lane[m.group(1)] = m.group(2)
        return None

    m = re.match(r"^\S+\s+(PASS|PARTIAL|FAIL|PARKED)\s+(\S+)\s+after\s+(\d+)s\s+\(attempt\s+(\d+),\s*(.*?)\)", line)
    if m:
        result, slug, secs, attempt, rest = m.groups()
        cost = _parse_cost(rest)
        tokens = _parse_tokens(line)
        agent = (last_lane or {}).get(slug)
        return {"time": line.split()[0], "slug": slug, "agent": agent or "unknown",
                "model": None, "attempt": int(attempt), "duration_s": int(secs),
                "tokens": tokens, "cost_usd": cost,
                "unit": None, "result": result}

    m = re.match(r"^\S+\s+review\s+(\S+)\s*:\s*\S+\s+after\s+(\d+)s(?:\s+\((.*)\))?", line)
    if m:
        slug, secs, rest = m.groups()
        return {"time": line.split()[0], "slug": slug, "agent": "review",
                "model": None, "attempt": None, "duration_s": int(secs),
                "tokens": {"in": None, "out": None, "cache_read": None, "cache_write": None},
                "cost_usd": _parse_cost(rest), "unit": None, "result": "review"}

    m = re.match(r"^\S+\s+merge\s+review(?:/)?([^:]+):\s*.*", line)
    if m:
        slug = m.group(1).strip()
        return {"time": line.split()[0], "slug": slug, "agent": "review",
                "model": None, "attempt": None, "duration_s": 0,
                "tokens": {"in": None, "out": None, "cache_read": None, "cache_write": None},
                "cost_usd": None, "unit": None, "result": "merge"}

    return None


def parse_runner_log(log_path: str | Path | None = None) -> list[dict]:
    path = Path(log_path) if log_path else (ROOT / load_config().get("credits_runner_log", ".review/runner.log"))
    if not path.is_absolute():
        path = ROOT / path
    entries: list[dict] = []
    last_lane: dict[str, str] = {}
    if not path.exists():
        return entries
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        entry = _parse_runner_log_entry(raw, last_lane)
        if entry is not None:
            entries.append(entry)
    return entries


def write_backfill(log_path: str | Path | None = None, ledger_path_value: str | Path | None = None,
                  cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load_config()
    target = Path(ledger_path_value) if ledger_path_value is not None else ledger_path(cfg)
    if not target.is_absolute():
        target = REVIEW / str(target)
    existing = read_ledger(target)
    seen = {(e.get("time"), e.get("slug"), e.get("agent"), e.get("attempt"), e.get("duration_s"))
            for e in existing}
    rows = []
    for entry in parse_runner_log(log_path):
        key = (entry.get("time"), entry.get("slug"), entry.get("agent"), entry.get("attempt"), entry.get("duration_s"))
        if key in seen:
            continue
        seen.add(key)
        rows.append(entry)
    if rows:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    return rows


def _sum(entries: list[dict], key: str) -> float:
    total = 0.0
    for entry in entries:
        value = entry.get(key)
        if value is None:
            continue
        try:
            total += float(value)
        except (TypeError, ValueError):
            continue
    return total


def _sum_token(entries: list[dict], field: str) -> int:
    total = 0
    for entry in entries:
        toks = entry.get("tokens") or {}
        val = toks.get(field)
        if isinstance(val, int):
            total += val
    return total


def summarize(entries: list[dict]) -> dict:
    out = {"entries": len(entries), "cost_usd": 0.0, "duration_s": 0, "tokens": {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}}
    for entry in entries:
        cost = entry.get("cost_usd")
        if cost is not None:
            try:
                out["cost_usd"] += float(cost)
            except (TypeError, ValueError):
                pass
        dur = entry.get("duration_s")
        if isinstance(dur, (int, float)):
            out["duration_s"] += int(dur)
        toks = entry.get("tokens") or {}
        for name in ("in", "out", "cache_read", "cache_write"):
            val = toks.get(name)
            if isinstance(val, int):
                out["tokens"][name] += val
    out["cost_usd"] = round(out["cost_usd"], 2)
    return out


def _by_group(entries: list[dict], field: str) -> list[dict]:
    groups: dict[str, dict] = {}
    for entry in entries:
        key = str(entry.get(field) or "unknown")
        g = groups.setdefault(key, {field: key, "runs": 0, "cost_usd": 0.0, "duration_s": 0, "tokens": {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}})
        g["runs"] += 1
        cost = entry.get("cost_usd")
        if cost is not None:
            try:
                g["cost_usd"] += float(cost)
            except (TypeError, ValueError):
                pass
        dur = entry.get("duration_s")
        if isinstance(dur, (int, float)):
            g["duration_s"] += int(dur)
        toks = entry.get("tokens") or {}
        for name in ("in", "out", "cache_read", "cache_write"):
            val = toks.get(name)
            if isinstance(val, int):
                g["tokens"][name] += val
    rows = []
    for key, group in groups.items():
        group["cost_usd"] = round(group["cost_usd"], 2)
        rows.append({field: key, **group})
    rows.sort(key=lambda r: (-float(r.get("cost_usd") or 0.0), -int(r.get("duration_s") or 0), r.get(field, "")))
    return rows


def totals_by_slug(entries: list[dict]) -> list[dict]:
    return [{"slug": row["slug"], "runs": row["runs"], "cost_usd": row["cost_usd"], "duration_s": row["duration_s"],
             "tokens": row["tokens"]} for row in _by_group(entries, "slug")]


def totals_by_agent(entries: list[dict]) -> list[dict]:
    return [{"agent": row["agent"], "runs": row["runs"], "cost_usd": row["cost_usd"], "duration_s": row["duration_s"],
             "tokens": row["tokens"]} for row in _by_group(entries, "agent")]


def totals_by_day(entries: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for entry in entries:
        key = str((entry.get("time") or "").split("T", 1)[0] or "unknown")
        g = groups.setdefault(key, {"day": key, "runs": 0, "cost_usd": 0.0, "duration_s": 0, "tokens": {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}})
        g["runs"] += 1
        cost = entry.get("cost_usd")
        if cost is not None:
            try:
                g["cost_usd"] += float(cost)
            except (TypeError, ValueError):
                pass
        dur = entry.get("duration_s")
        if isinstance(dur, (int, float)):
            g["duration_s"] += int(dur)
        toks = entry.get("tokens") or {}
        for name in ("in", "out", "cache_read", "cache_write"):
            val = toks.get(name)
            if isinstance(val, int):
                g["tokens"][name] += val
    rows = [{"day": d, "runs": g["runs"], "cost_usd": round(g["cost_usd"], 2), "duration_s": g["duration_s"], "tokens": g["tokens"]} for d, g in groups.items()]
    rows.sort(key=lambda r: r["day"])
    return rows


def totals_by_week(entries: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for entry in entries:
        ts = str(entry.get("time") or "").split("T", 1)[0]
        try:
            day = dt.date.fromisoformat(ts)
        except ValueError:
            continue
        iso = day.isocalendar()
        key = f"{iso[0]}-W{iso[1]:02d}"
        g = groups.setdefault(key, {"week": key, "runs": 0, "cost_usd": 0.0, "duration_s": 0, "tokens": {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}})
        g["runs"] += 1
        cost = entry.get("cost_usd")
        if cost is not None:
            try:
                g["cost_usd"] += float(cost)
            except (TypeError, ValueError):
                pass
        dur = entry.get("duration_s")
        if isinstance(dur, (int, float)):
            g["duration_s"] += int(dur)
        toks = entry.get("tokens") or {}
        for name in ("in", "out", "cache_read", "cache_write"):
            val = toks.get(name)
            if isinstance(val, int):
                g["tokens"][name] += val
    rows = [{"week": w, "runs": g["runs"], "cost_usd": round(g["cost_usd"], 2), "duration_s": g["duration_s"], "tokens": g["tokens"]} for w, g in groups.items()]
    rows.sort(key=lambda r: r["week"])
    return rows


def top_expensive(entries: list[dict], limit: int = 10) -> list[dict]:
    rows = totals_by_slug(entries)
    rows.sort(key=lambda r: (-float(r.get("cost_usd") or 0.0), -int(r.get("duration_s") or 0), r.get("slug", "")))
    return rows[:limit]


def today_totals(entries: list[dict], day: str | None = None) -> dict:
    day = day or dt.date.today().isoformat()
    rows = [e for e in entries if (e.get("time") or "").startswith(day)]
    return summarize(rows)


def _cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summaries for Orchestra task credits")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of plain text")
    parser.add_argument("--slug", default=None, help="Filter to one slug")
    parser.add_argument("--agent", default=None, help="Filter to one lane/agent")
    parser.add_argument("--log", default=None, help="Custom runner log path (fixture mode)")
    parser.add_argument("--ledger", default=None, help="Custom ledger path (fixture mode)")
    parser.add_argument("--backfill", action="store_true", help="Backfill the ledger from runner.log")
    return parser.parse_args()


def _filtered(entries: list[dict], slug: str | None = None, agent: str | None = None) -> list[dict]:
    out = entries
    if slug:
        out = [e for e in out if str(e.get("slug")) == slug]
    if agent:
        out = [e for e in out if str(e.get("agent")) == agent]
    return out


def _plain(entries: list[dict], slug: str | None = None, agent: str | None = None) -> str:
    rows = _filtered(entries, slug=slug, agent=agent)
    total = summarize(rows)
    lines = [
        f"entries: {len(rows)}",
        f"cost_usd: ${total['cost_usd']:.2f}",
        f"duration_s: {total['duration_s']}",
        f"tokens: in={total['tokens']['in']} out={total['tokens']['out']} cache_read={total['tokens']['cache_read']} cache_write={total['tokens']['cache_write']}",
    ]
    for row in totals_by_agent(rows):
        lines.append(f"agent {row['agent']}: runs={row['runs']} cost_usd=${row['cost_usd']:.2f} duration_s={row['duration_s']}")
    for row in totals_by_slug(rows)[:10]:
        lines.append(f"slug {row['slug']}: runs={row['runs']} cost_usd=${row['cost_usd']:.2f} duration_s={row['duration_s']}")
    return "\n".join(lines)


def _json(entries: list[dict], slug: str | None = None, agent: str | None = None) -> dict:
    rows = _filtered(entries, slug=slug, agent=agent)
    return {
        "summary": summarize(rows),
        "by_agent": totals_by_agent(rows),
        "by_slug": totals_by_slug(rows),
        "by_day": totals_by_day(rows),
        "by_week": totals_by_week(rows),
        "top_tasks": top_expensive(rows),
        "today": today_totals(rows),
    }


def main() -> int:
    args = _cli()
    cfg = load_config()
    log_path = Path(args.log) if args.log else (ROOT / cfg.get("credits_runner_log", ".review/runner.log"))
    target = Path(args.ledger) if args.ledger else ledger_path(cfg)
    if args.backfill:
        write_backfill(log_path, target, cfg)
    entries = read_ledger(target)
    if args.slug or args.agent:
        entries = _filtered(entries, slug=args.slug, agent=args.agent)
    if args.json:
        print(json.dumps(_json(entries, slug=args.slug, agent=args.agent), indent=2, ensure_ascii=False))
    else:
        print(_plain(entries, slug=args.slug, agent=args.agent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
