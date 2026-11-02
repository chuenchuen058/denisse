"""Orchestra on one screen: what is running, what is queued and why it waits, what is
parked and why, today's results and spend, and the last lines of the log.

    .venv\\Scripts\\python.exe orchestra\\status.py          # for people
    .venv\\Scripts\\python.exe orchestra\\status.py --json   # for machines
Reads only; changes nothing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner as R  # noqa: E402

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
            "", "Log (last 5):"]
    out += [f"  {l}" for l in s["log_tail"]] or ["  (empty)"]
    if s["parked"]:
        out += ["", "Re-queue a parked task: python orchestra\\requeue.py <slug> --note \"why\""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)
    s = collect()
    print(json.dumps(s, indent=2) if a.json else render(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
