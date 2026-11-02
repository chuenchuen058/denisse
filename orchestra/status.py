"""Orchestra on one screen: what is running, what is queued and why it waits, what is
parked and why, today's results and spend, and the last lines of the log.

    .venv\\Scripts\\python.exe orchestra\\status.py          # for people
    .venv\\Scripts\\python.exe orchestra\\status.py --json   # for machines
    .venv\\Scripts\\python.exe orchestra\\status.py --by-prompt  # results per review_prompt.md version
Reads only; changes nothing. Tokens shown are telemetry (what a run used), not an allowance.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner as R  # noqa: E402

BY_PROMPT_DAYS = 14
BY_PROMPT_RESULTS = ("PASS", "PARTIAL", "FAIL", "NO_REPORT")
RECENT_RUNS = 5


def by_prompt(state: dict, days: int = BY_PROMPT_DAYS) -> list[dict]:
    """Runs of the last `days` days grouped by prompt_file_sha (None: run before it was recorded).
    PASS/PARTIAL/FAIL/NO_REPORT counts; anything else (TIMEOUT, LIMITED) under `other`.
    Median minutes over every run in the group. Newest-used version first."""
    since = R.now() - dt.timedelta(days=days)
    groups: dict = {}
    for ts in state.get("tasks", {}).values():
        for h in ts.get("history", []):
            try:
                end = dt.datetime.fromisoformat(str(h.get("end")))
            except ValueError:
                continue
            if end < since:
                continue
            g = groups.setdefault(h.get("prompt_file_sha"), {"prompt_file_sha": h.get("prompt_file_sha"),
                                                            **{r: 0 for r in BY_PROMPT_RESULTS},
                                                            "other": 0, "runs": 0, "_mins": [], "last": ""})
            res = h.get("result")
            g[res if res in BY_PROMPT_RESULTS else "other"] += 1
            g["runs"] += 1
            if isinstance(h.get("seconds"), (int, float)):
                g["_mins"].append(h["seconds"] / 60)
            g["last"] = max(g["last"], end.isoformat())
    out = []
    for g in groups.values():
        mins = g.pop("_mins")
        g["median_min"] = round(statistics.median(mins), 1) if mins else None
        out.append(g)
    return sorted(out, key=lambda g: g["last"], reverse=True)


def render_by_prompt(rows: list[dict], days: int = BY_PROMPT_DAYS) -> str:
    out = [f"Results by review_prompt.md version, last {days} days",
           f"  {'prompt_file_sha':<15} {'PASS':>5} {'PARTIAL':>8} {'FAIL':>5} {'NO_REPORT':>10} "
           f"{'other':>6} {'median min':>11}  last run"]
    for g in rows:
        med = "-" if g["median_min"] is None else f"{g['median_min']:.1f}"
        out.append(f"  {g['prompt_file_sha'] or '(unrecorded)':<15} {g['PASS']:>5} {g['PARTIAL']:>8} "
                   f"{g['FAIL']:>5} {g['NO_REPORT']:>10} {g['other']:>6} {med:>11}  {g['last'][:16]}")
    if not rows:
        out.append("  no runs")
    return "\n".join(out)


def recent_runs(state: dict, n: int = RECENT_RUNS) -> list[dict]:
    """The last n history entries across tasks, newest first, with their provenance."""
    runs = [{"slug": slug, **h} for slug, ts in state.get("tasks", {}).items() for h in ts.get("history", [])]
    runs.sort(key=lambda h: str(h.get("end", "")), reverse=True)
    keep = ("slug", "end", "result", "lane", "seconds", "spent", "prompt_sha", "prompt_file_sha",
            "base_commit", "model", "tokens", "session_id")
    return [{k: h.get(k) for k in keep} for h in runs[:n]]


LOCK_NAME = re.compile(r"^runner(?:\.(?P<lane>[^.]+)(?:\.(?P<slot>\d+))?)?\.lock$")


def _running() -> list[dict]:
    out = []
    for lf in sorted(R.REVIEW.glob("runner*.lock")):
        m = LOCK_NAME.match(lf.name)
        if not m:
            continue
        try:
            lk = json.loads(lf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            lk = {}
        out.append({"lane": m.group("lane") or "(single)", "slot": int(m.group("slot") or 1),
                    "task": lk.get("task"), "since": lk.get("since"), "pid": lk.get("pid"),
                    "lock": lf.name})
    return out


def collect() -> dict:
    state = R.load_state()
    today = R.now().date().isoformat()

    queue = []
    for t in R.load_queue() if R.QUEUE.exists() else []:
        attempts = state["tasks"].get(t.slug, {}).get("attempts", 0)
        queue.append({"slug": t.slug, "title": t.title, "lane": t.lane, "priority": t.priority,
                      "kind": t.kind, "attempts": attempts,
                      "why_not_runnable": R.why_not_runnable(t, state)})

    parked = []
    for p in sorted(R.PARKED.glob("*.md")) if R.PARKED.exists() else []:
        if p.name.endswith(".report.md"):
            continue
        why_file = R.PARKED / f"{p.stem}.why.txt"
        why = ""
        if why_file.exists():
            why = (why_file.read_text(encoding="utf-8").splitlines() or [""])[0]
        parked.append({"slug": p.stem, "why": why})

    results: dict[str, int] = {}
    for ts in state["tasks"].values():
        for h in ts.get("history", []):
            if str(h.get("end", "")).startswith(today):
                results[h.get("result", "?")] = results.get(h.get("result", "?"), 0) + 1
    u = state["days"].get(today, {})

    log_tail = []
    if R.LOG_FILE.exists():
        log_tail = R.LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]

    try:
        import power
        ps = power.power_status()
        pw = {**ps, "dispatch_paused": bool(power.dispatch_block(ps, R.C)), "text": power.describe(ps, R.C)}
    except Exception as e:  # reads only; a power reading must never break status
        pw = {"ac": None, "battery_percent": None, "dispatch_paused": False, "text": f"unknown ({e})"}

    return {"generated": R.now().isoformat(), "power": pw, "running": _running(), "queue": queue, "parked": parked,
            "recent": recent_runs(state),
            "today": {"date": today, "results": results, "cost_usd": round(u.get("cost_usd", 0), 2),
                      "spend_cap_usd": R.C["daily_spend_cap_usd"],
                      "estimated_runs": u.get("estimated_runs", 0),
                      "work_min": u.get("work_s", 0) // 60, "maint_min": u.get("maint_s", 0) // 60},
            "log_tail": log_tail}


def render(s: dict) -> str:
    out = [f"Orchestra status {s['generated']}", f"power: {s['power']['text']}", "", "Running:"]
    out += [f"  {r['lane']} slot {r['slot']}: {r['task'] or '(starting)'} since {r['since'] or '?'}"
            for r in s["running"]] or ["  nothing"]
    out += ["", f"Queue ({len(s['queue'])}):"]
    out += [f"  {q['slug']} [{q['priority']}/{q['kind']}/{q['lane']}, attempts {q['attempts']}]: "
            f"{q['why_not_runnable'] or 'runnable'}" for q in s["queue"]] or ["  empty"]
    out += ["", f"Parked ({len(s['parked'])}):"]
    out += [f"  {p['slug']}: {p['why'] or '(no why file)'}" for p in s["parked"]] or ["  none"]
    t = s["today"]
    counts = ", ".join(f"{k} {v}" for k, v in sorted(t["results"].items())) or "no runs"
    est = f" ({t['estimated_runs']} estimated)" if t["estimated_runs"] else ""
    out += ["", f"Today {t['date']}: {counts}",
            f"  spend ${t['cost_usd']:.2f} of ${t['spend_cap_usd']:.2f}{est}; "
            f"{t['work_min']} min work, {t['maint_min']} min maintenance",
            "", f"Recent runs (last {RECENT_RUNS}):"]
    out += [f"  {r['end'] or '?'} {r['result']} {r['slug']} lane {r['lane'] or '?'} "
            f"model {r['model'] or '?'} prompt {r['prompt_sha'] or '?'} {R.fmt_tokens(r['tokens'])}"
            for r in s.get("recent", [])] or ["  none"]
    out += ["", "Log (last 5):"]
    out += [f"  {l}" for l in s["log_tail"]] or ["  (empty)"]
    if s["parked"]:
        out += ["", "Re-queue a parked task: python orchestra\\requeue.py <slug> --note \"why\""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--by-prompt", action="store_true",
                    help=f"PASS/PARTIAL/FAIL/NO_REPORT and median minutes per review_prompt.md version, "
                         f"last {BY_PROMPT_DAYS} days")
    a = ap.parse_args(argv)
    if a.by_prompt:
        rows = by_prompt(R.load_state())
        print(json.dumps(rows, indent=2) if a.json else render_by_prompt(rows))
        return 0
    s = collect()
    print(json.dumps(s, indent=2) if a.json else render(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
