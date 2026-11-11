"""Orchestra picks a task's lane, and a change's reviewer, itself at dispatch time.

    python -m orchestra.lane_pick explain <slug>      # the scores per lane and why the winner won
    python -m orchestra.lane_pick migrate --dry-run   # queued tasks whose Lane: could be auto

Chuen, 11 Nov 2026: "These things are hardcoded. I want everything softcoded, where the agents
auto-task." Every number, lane name and reset time below comes from orchestra/config.json
(lane_strengths, lane_pacing, lane_pick_weights, lane_preference_bonus, bugfix_lane_bonus,
reviewer_lanes, ...); the live state (limits, busy slots, spend, usage) is passed in. The
functions here are pure: the runner builds a `Live` from its state and calls `choose`.

How a lane is scored (higher wins; only *available* lanes can win):
  fit        lane_strengths[lane][work kind] (lane_strength_default when not listed)
  preference lane_preference_bonus for the lane the header names (Lane: X, not auto)
  bug fix    bugfix_lane_bonus for bugfix_lane when the task's Kind is a bug fix
  pacing     lane_pacing: spend in proportion to the allowance left against the time left
             before the lane's reset; within spend_window_h of the reset, use it or lose it;
             at or under `reserve` of the allowance left (reserve 1 = keep it all), hold
  cost       a priced lane (not in unpriced_lanes) costs the share of daily_spend_cap_usd spent
  busy       a lane with no free slot loses a little, so work spreads
  hint       the lane_fallback of the asked (or best-fit) lane, when that one is unavailable
A lane is unavailable when it is not configured, limited (state lanes.<lane>.limited_until),
over the spend cap (priced lanes), or held by pacing (except the lane the header names:
naming a lane is Chuen's own pacing decision). Lane-Fixed: yes never moves.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent


@dataclass
class Ask:
    """What a task asks for, as far as the lane is concerned."""
    slug: str
    asked: str               # the header's Lane: value, lower case (the auto word or a lane)
    work: str                # the work kind the strengths are looked up by
    bugfix: bool = False
    fixed: bool = False      # Lane-Fixed: yes
    author: str | None = None  # reviewer picks: the lane that wrote the change


@dataclass
class Live:
    """The live state a pick is made from."""
    now: dt.datetime
    lanes: list[str]                                        # configured lanes, in config order
    limited: dict[str, dt.datetime] = field(default_factory=dict)
    busy: dict[str, int] = field(default_factory=dict)      # running tasks per lane
    slots: dict[str, int] = field(default_factory=dict)
    spend_left: float | None = None                         # None: no cap known
    usage: dict[str, dict] = field(default_factory=dict)    # .review/<lane_usage_file>, per lane


@dataclass
class Pick:
    lane: str | None
    why: str
    scores: list[dict]
    available: bool          # False: no candidate could run now (lane is the best of the rest)
    moved: str = ""          # "", "fallback" (the asked lane is limited), "bugfix", "picked"


# ---------------------------------------------------------------- small helpers

def auto_word(cfg: dict) -> str:
    return str(cfg.get("lane_auto_word") or "").lower()


def is_auto(lane: str | None, cfg: dict) -> bool:
    return bool(auto_word(cfg)) and (lane or "").lower() == auto_word(cfg)


def is_yes(value: str | None, cfg: dict) -> bool:
    return (value or "").strip().lower() in [str(w).lower() for w in cfg.get("lane_pick_yes_words", [])]


def parse_when(text) -> dt.datetime | None:
    try:
        when = dt.datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return when.replace(tzinfo=None) if when.tzinfo else when


def next_reset(anchor, period_h: float, at: dt.datetime) -> dt.datetime | None:
    """The first reset after `at`, given one reset time (`anchor`) and the period between resets."""
    a = parse_when(anchor)
    if a is None or not period_h or period_h <= 0:
        return None
    period = dt.timedelta(hours=float(period_h))
    if a > at:
        n = int((a - at) / period)
        return a - n * period
    n = int((at - a) / period) + 1
    return a + n * period


def usage_left(entry: dict | None, at: dt.datetime, max_age_h: float) -> float | None:
    """Share of the allowance left (0..1) from a usage record, or None when unknown or stale."""
    if not isinstance(entry, dict):
        return None
    seen = parse_when(entry.get("at"))
    if seen is not None and max_age_h and (at - seen).total_seconds() > float(max_age_h) * 3600:
        return None
    for key, flip in (("left", False), ("used", True)):
        v = entry.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = max(0.0, min(1.0, float(v)))
            return 1.0 - v if flip else v
    return None


def pacing(lane: str, cfg: dict, live: Live) -> tuple[float, bool, str]:
    """(score -1..1, hold, why) for spending this lane now. No pacing entry: (0, False, '')."""
    p = (cfg.get("lane_pacing") or {}).get(lane)
    if not isinstance(p, dict):
        return 0.0, False, ""
    entry = live.usage.get(lane) if isinstance(live.usage, dict) else None
    period_h = float(p.get("period_h") or 0)
    anchor = (entry or {}).get("reset") or p.get("reset_anchor")
    reset = next_reset(anchor, period_h, live.now) if anchor else None
    left = usage_left(entry, live.now, cfg.get("lane_usage_max_age_h", 0))
    reserve = float(p.get("reserve") or 0)
    window = float(p.get("spend_window_h") or 0)
    hours = (reset - live.now).total_seconds() / 3600 if reset else None
    if hours is not None and hours <= window:
        if left is not None and left <= 0:
            return 0.0, True, f"allowance used up; reset in {hours:.0f}h"
        return 1.0, False, f"use it or lose it: reset in {hours:.0f}h"
    when = f"reset in {hours:.0f}h" if hours is not None else "reset time unknown"
    # Chuen, 11 Nov 2026: "the holding back only applies if the weekly limit is less than
    # 20%; if it's not, use them freely". At or above lane_free_above_left (per lane
    # "free_above_left" wins) the lane is neither held nor paced; unknown usage counts as
    # not low. Below it, reserve and pacing apply as before. Key absent = old behaviour.
    free = p.get("free_above_left", cfg.get("lane_free_above_left"))
    if isinstance(free, (int, float)) and not isinstance(free, bool):
        if left is None:
            return 0.0, False, f"usage unknown, used freely ({when})"
        if left >= float(free):
            return 0.0, False, f"{left:.0%} left, used freely above {float(free):.0%} ({when})"
    if reserve >= 1:
        return -1.0, True, f"held until the last {window:g}h before its reset ({when})"
    if left is None:
        return 0.0, False, f"usage unknown ({when})"
    if left <= reserve:
        return -1.0, True, f"{left:.0%} left, at its {reserve:.0%} reserve ({when})"
    spendable = (left - reserve) / (1 - reserve)
    if hours is None or not period_h:
        return 0.0, False, f"{left:.0%} left ({when})"
    behind = spendable - min(1.0, hours / period_h)
    return max(-1.0, min(1.0, behind)), False, f"{left:.0%} left, {when}"


def fit(lane: str, work: str, cfg: dict) -> float:
    per = (cfg.get("lane_strengths") or {}).get(lane)
    default = float(cfg.get("lane_strength_default", 0))
    if not isinstance(per, dict):
        return default
    v = per.get(work)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def work_kind(fields: dict, title: str, kind: str, cfg: dict, category=None) -> str:
    """The work kind for the strengths: a Work: header, else the task's Kind mapped
    (lane_work_for_kind), else the title's category (category(title)), else lane_work_default."""
    w = (fields.get(str(cfg.get("lane_work_header", "")).lower()) or "").strip().lower()
    if w:
        return w
    mapped = (cfg.get("lane_work_for_kind") or {}).get((kind or "").lower())
    if mapped:
        return str(mapped)
    if category is not None:
        try:
            got = category(title)
        except Exception:
            got = None
        if got:
            return str(got)
    return str(cfg.get("lane_work_default", ""))


# ---------------------------------------------------------------- scoring

def availability(lane: str, ask: Ask, cfg: dict, live: Live, held: bool, held_why: str) -> str | None:
    """Why `lane` can't take this task now, or None if it can."""
    if lane not in live.lanes:
        return "not a configured lane"
    until = live.limited.get(lane)
    if until and until > live.now:
        return f"limited until {until:%H:%M}"
    if lane not in cfg.get("unpriced_lanes", []) and live.spend_left is not None and live.spend_left <= 0:
        return f"daily spend cap ${float(cfg.get('daily_spend_cap_usd', 0)):.2f} reached"
    # A pacing hold doesn't apply to the lane the header names (Chuen's own call), nor to a
    # review: a review is short, and with no independent reviewer a good PASS would park.
    # Pacing still weighs on both through the score.
    if held and not ask.author and lane != ask.asked:
        return f"conserving: {held_why}"
    if ask.author and lane == ask.author:
        return "wrote the change (the review must be independent)"
    return None


def candidates(ask: Ask, cfg: dict, live: Live, pool: list[str]) -> list[str]:
    """The lanes this task may go to. Lane-Fixed: only its own. Lane: auto (or a reviewer pick):
    the pool. An explicit Lane: with lane_pick_explicit_scope "any": the pool; otherwise only the
    asked lane, the bug-fix lane for a bug fix and, while the asked lane can't run, its
    lane_fallback hint (today's rules, as weights)."""
    if ask.fixed and not ask.author:
        return [ask.asked]
    if ask.author or is_auto(ask.asked, cfg) or cfg.get("lane_pick_explicit_scope") == "any":
        return [l for l in pool if l in live.lanes]
    out = [ask.asked]
    bug = str(cfg.get("bugfix_lane") or "")
    if ask.bugfix and bug and bug in live.lanes:
        out.append(bug)
    hint = (cfg.get("lane_fallback") or {}).get(ask.asked)
    _pace, held, why = pacing(ask.asked, cfg, live)
    if hint and hint in live.lanes and availability(ask.asked, ask, cfg, live, held, why):
        out.append(hint)
    return list(dict.fromkeys(out))


def score(ask: Ask, cfg: dict, live: Live, pool: list[str] | None = None) -> list[dict]:
    """One row per candidate lane: total, its parts, and why it is unavailable (None if it isn't)."""
    w = cfg.get("lane_pick_weights") or {}
    pool = pool if pool is not None else [l for l in live.lanes if l != cfg.get("reviewer_override_lane")]
    lanes = candidates(ask, cfg, live, pool)
    explicit = not is_auto(ask.asked, cfg) and not ask.author
    rows = []
    for lane in lanes:
        pace, held, pace_why = pacing(lane, cfg, live)
        parts = {"fit": float(w.get("fit", 0)) * fit(lane, ask.work, cfg)}
        if explicit and lane == ask.asked:
            parts["preference"] = float(cfg.get("lane_preference_bonus", 0))
        if ask.bugfix and not ask.author and lane == cfg.get("bugfix_lane"):
            parts["bugfix"] = float(cfg.get("bugfix_lane_bonus", 0))
        if pace and not (explicit and lane == ask.asked):  # naming a lane is Chuen's own pacing call
            parts["pacing"] = float(w.get("pacing", 0)) * pace
        cap = float(cfg.get("daily_spend_cap_usd", 0) or 0)
        if lane not in cfg.get("unpriced_lanes", []) and cap > 0 and live.spend_left is not None:
            parts["cost"] = -float(w.get("cost", 0)) * max(0.0, min(1.0, 1 - live.spend_left / cap))
        if live.busy.get(lane, 0) >= int(live.slots.get(lane, 1) or 1):
            parts["busy"] = -float(w.get("busy", 0))
        rows.append({"lane": lane, "parts": parts, "pacing_why": pace_why,
                     "unavailable": availability(lane, ask, cfg, live, held, pace_why)})
    # The lane_fallback hint: a tie-break toward the fallback of the asked (or best-fit) lane
    # when that lane can't run.
    first = ask.asked if explicit else max(rows, key=lambda r: r["parts"]["fit"])["lane"] if rows else None
    first_row = next((r for r in rows if r["lane"] == first), None)
    hint = (cfg.get("lane_fallback") or {}).get(first) if first_row and first_row["unavailable"] else None
    for r in rows:
        if hint and r["lane"] == hint:
            r["parts"]["hint"] = float(w.get("fallback_hint", 0))
        r["total"] = round(sum(r["parts"].values()), 4)
    order = {l: i for i, l in enumerate(live.lanes)}
    rows.sort(key=lambda r: (r["unavailable"] is not None, -r["total"], order.get(r["lane"], len(order))))
    return rows


def _why(row: dict) -> str:
    bits = [f"{k} {v:+.2f}" for k, v in row["parts"].items() if v]
    if row.get("pacing_why"):
        bits.append(row["pacing_why"])
    return f"score {row['total']:.2f} ({', '.join(bits) or 'no score'})"


def choose(ask: Ask, cfg: dict, live: Live, pool: list[str] | None = None) -> Pick:
    """The lane for this task (or reviewer) now, with the scores and a one-line why."""
    rows = score(ask, cfg, live, pool)
    if not rows:
        return Pick(None, "no candidate lane", rows, False)
    best = rows[0]
    if best["unavailable"]:
        why = "no lane available: " + "; ".join(f"{r['lane']} {r['unavailable']}" for r in rows)
        keep = ask.asked if (not is_auto(ask.asked, cfg) and not ask.author) else best["lane"]
        return Pick(keep, why, rows, False)
    lane = best["lane"]
    others = [f"{r['lane']} {r['unavailable']}" for r in rows[1:] if r["unavailable"]]
    why = f"{ask.work or 'work'}: {lane} {_why(best)}"
    if others:
        why += "; " + "; ".join(others)
    moved = ""
    if lane != ask.asked and not ask.author:
        asked_row = next((r for r in rows if r["lane"] == ask.asked), None)
        if is_auto(ask.asked, cfg):
            moved = "picked"
        elif asked_row and (asked_row["unavailable"] or "").startswith("limited"):
            moved = "fallback"
        elif ask.bugfix and lane == cfg.get("bugfix_lane"):
            moved = "bugfix"
        else:
            moved = "picked"
    return Pick(lane, why, rows, True, moved)


def table(rows: list[dict]) -> str:
    """The scores as text lines, winner first."""
    out = []
    for r in rows:
        tail = f"  [unavailable: {r['unavailable']}]" if r["unavailable"] else ""
        out.append(f"    {r['lane']:<10} {_why(r)}{tail}")
    return "\n".join(out)


# ---------------------------------------------------------------- command line

def _runner():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import runner
    return runner


def explain(slug: str) -> int:
    R = _runner()
    path = R.QUEUE / f"{slug}.md"
    if not path.exists():
        print(f"no queued task {slug}")
        return 1
    task = R.parse_task(path)
    state = R.load_state()
    p = R.lane_pick_for(task, state)
    print(f"{slug}: asked Lane: {task.fields.get('lane', '')}; work {R.lane_ask(task).work}")
    print(table(p.scores))
    print(f"-> {p.lane}: {p.why}")
    return 0


def migrate(dry_run: bool) -> int:
    """Queued tasks whose lane could be auto: an explicit lane without Lane-Fixed. Lists only;
    task files are never rewritten."""
    R = _runner()
    found = 0
    for task in R.load_queue():
        lane = task.fields.get("lane", "")
        if is_auto(lane, R.C) or is_yes(task.fields.get("lane-fixed"), R.C):
            continue
        found += 1
        print(f"{task.slug}: Lane: {lane} could be Lane: {auto_word(R.C)}")
    print(f"{found} task(s) could be {auto_word(R.C)}; nothing was changed"
          + ("" if dry_run else " (this helper only lists; edit the files yourself)"))
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("explain", help="the scores per lane for one queued task")
    e.add_argument("slug")
    m = sub.add_parser("migrate", help="list queued tasks whose lane could be auto (never rewrites them)")
    m.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "explain":
        return explain(a.slug)
    return migrate(a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
