"""Each lane's usage allowance: what is left in each window, when it resets, and what the
runner should do about it -- spend it down before it expires, conserve it, or just spread.

    .venv\\Scripts\\python.exe orchestra\\allowance.py show            # every lane, estimated now
    .venv\\Scripts\\python.exe orchestra\\allowance.py set codex --left 10% --resets 2026-11-11T12:00
    .venv\\Scripts\\python.exe orchestra\\allowance.py set codex --left 10% --resets "Wed 12:00" --window weekly
    .venv\\Scripts\\python.exe orchestra\\allowance.py learn           # read reset times from old lane logs

Chuen, 9 Nov 2026: "Since Codex usage is only 10% for 2 days until it resets weekly, I'd wait
till the last few hours to use it." Before this the runner only reacted (LIMITED, wait, fall back).

The model (config "allowance": lane -> windows {hours, reset, run_pct}, model_factor) is data; so are the
planner's thresholds (allowance_*). Evidence goes into .review/allowance.json (allowance_file):
  * what Chuen states (`set`),
  * what a lane reports itself: Claude's stream-json rate_limit_event carries every window's
    utilization and resetsAt, so each claude run is a fresh reading,
  * each LIMITED (that window is at 0 until the time the error names; the window is the one
    named in the message, else the shortest window long enough to hold that wait),
  * each run (lane, weight, model, tokens, cost, turns, seconds), which is subtracted from the
    last reading at the config's estimated cost per run until a newer reading arrives.
A lane with no reading for a window is "unknown" there and is never held for it.

Planner, per lane (estimate()):
  held      the runner's own LIMITED wait (passed in);
  conserve  a window is below allowance_conserve_below_pct and its reset is further away than
            allowance_spend_down_hours: only urgent tasks may use it, and only down to
            allowance_reserve_pct;
  spend     a window still has allowance and resets within allowance_spend_down_hours ("use it
            or lose it"): preferred when the planner chooses a lane;
  spread    otherwise (or unknown).
And per task: a lane with less left in any window than the task's estimated cost there (the
window's run_pct by weight, times the model's model_factor) is held for it. In dispatch the
runner hands both to orchestra/lane_pick.py: a held lane is unavailable for that task, a
spend-down lane scores allowance_spend_bonus (Lane: auto). choose() is the plan on its own (ties
between spend-down lanes: the soonest reset, then the most left, then candidate order).

This module reads no runner globals: runner.py imports it and passes cfg, the file and the clock.
Only the lock borrows the runner's process-liveness rule (runner._alive / proc_created), as
campaign.py does, so a live writer's lock is never reclaimed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ---------------------------------------------------------------- file

class LockBusy(TimeoutError):
    """Another writer held the allowance lock for longer than the wait: the update is skipped."""


def _owner_alive(pid, created) -> bool:
    """The runner's own liveness rule (runner._alive: the pid exists and, when a creation time was
    recorded, it is that very process; a reused pid is not the old writer). Tests replace it."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import runner
    return runner._alive(pid, created)


def _created(pid: int):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import runner
    return runner.proc_created(pid)


class _Lock:
    """A tiny cross-process lock (O_EXCL file), so two lanes finishing together don't lose an event.
    The file holds the holder's pid, its creation time and a token unique to this holder: only the
    holder deletes it on exit. A lock not acquired within `wait` raises LockBusy (the caller skips
    its write rather than writing unlocked over another writer's). A lock older than `stale`
    seconds is taken over only when its owner is gone (review, 16 Nov 2026: a slow learn_logs()
    holds it for longer than that while alive); a lock that names no owner (unreadable, or written
    before owners were recorded) is judged by its age alone."""

    def __init__(self, path: Path, wait: float = 10, stale: float = 60, alive=None):
        self.path, self.wait, self.stale = path.with_suffix(".lock"), wait, stale
        self.alive = alive or _owner_alive
        try:
            created = _created(os.getpid())
        except Exception:  # noqa: BLE001 -- no creation time: judged by the pid alone
            created = None
        self.token = json.dumps({"pid": os.getpid(), "created": created,
                                 "token": f"{os.getpid()}-{time.time_ns()}-{id(self)}"})
        self.owned = False

    def _read(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _owner_gone(self, text: str | None) -> bool:
        """True unless the lock names an owner that is still running."""
        try:
            info = json.loads(text or "")
            pid = info["pid"] if isinstance(info, dict) else None
        except (ValueError, KeyError, TypeError):
            return True
        if not isinstance(pid, int) or isinstance(pid, bool):
            return True
        try:
            return not self.alive(pid, info.get("created"))
        except Exception:  # noqa: BLE001 -- can't tell: a live writer's lock is never taken
            return False

    def __enter__(self):
        end = time.time() + self.wait
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, self.token.encode("utf-8"))
                finally:
                    os.close(fd)
                self.owned = True
                return self
            except FileExistsError:
                try:
                    seen = self._read()
                    if (time.time() - self.path.stat().st_mtime > self.stale and self._owner_gone(seen)
                            and self._read() == seen):
                        self.path.unlink(missing_ok=True)  # stale: its holder is gone
                        continue
                except OSError:
                    pass
                if time.time() > end:
                    raise LockBusy(f"{self.path.name} held by another writer for over {self.wait:g}s")
                time.sleep(0.05)

    def __exit__(self, *exc):
        if not self.owned:
            return
        self.owned = False
        try:
            if self._read() == self.token:  # never delete a lock another writer took over
                self.path.unlink(missing_ok=True)
        except OSError:
            pass


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("readings", {})
    data.setdefault("events", [])
    return data


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    for i in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 39:
                raise
            time.sleep(0.05)


def _update(path: Path, cfg: dict, fn) -> dict:
    """Load, change (fn), trim and save under the lock. LockBusy when the lock can't be had:
    nothing is written (the runner logs it and goes on; one event is lost, none overwritten)."""
    with _Lock(path, float(cfg.get("allowance_lock_wait_s", 10)), float(cfg.get("allowance_lock_stale_s", 60))):
        data = load(path)
        fn(data)
        keep = int(cfg.get("allowance_history_max", 500))
        data["events"] = data["events"][-keep:] if keep else []
        _save(path, data)
    return data


# ---------------------------------------------------------------- model

def spec(cfg: dict, lane: str) -> dict:
    return (cfg.get("allowance") or {}).get(lane) or {}


def windows(cfg: dict, lane: str) -> dict:
    return spec(cfg, lane).get("windows") or {}


def model_factor(cfg: dict, lane: str, model: str | None) -> float:
    """The lane's model_factor for `model` (a key contained in the model name, e.g. "sonnet" in
    "claude-sonnet-5-5"), else 1."""
    table = spec(cfg, lane).get("model_factor") or {}
    m = (model or "").lower()
    for key, f in table.items():
        if key.lower() in m:
            return float(f)
    return 1.0


def run_cost(cfg: dict, lane: str, window: str, weight: str, model: str | None = None) -> float | None:
    """Estimated percent of `window` one run of this weight uses (the window's run_pct, times
    the model's factor), or None if not configured."""
    pct = ((windows(cfg, lane).get(window) or {}).get("run_pct") or {}).get(weight)
    return None if pct is None else float(pct) * model_factor(cfg, lane, model)


def parse_when(text: str, at: dt.datetime) -> dt.datetime | None:
    """'2026-11-11T12:00' (local), or '<weekday> HH:MM' (the next one after `at`)."""
    s = str(text or "").strip()
    try:
        return dt.datetime.fromisoformat(s).replace(tzinfo=None, microsecond=0)
    except ValueError:
        pass
    m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}):(\d{2})", s)
    if not m or m.group(1).lower() not in _DAYS:
        return None
    day, h, mi = _DAYS.index(m.group(1).lower()), int(m.group(2)), int(m.group(3))
    if h > 23 or mi > 59:
        return None
    when = at.replace(hour=h, minute=mi, second=0, microsecond=0) + dt.timedelta(days=(day - at.weekday()) % 7)
    return when if when > at else when + dt.timedelta(days=7)


def _repeats(win: dict) -> bool:
    """A window with a fixed cadence: once one reset is known the next is `hours` later.
    reset "rolling" = starts again from first use, so after a reset the next one is unknown."""
    return str(win.get("reset", "learned")).lower() != "rolling" and float(win.get("hours") or 0) > 0


def _scheduled(win: dict, at: dt.datetime) -> dt.datetime | None:
    r = str(win.get("reset", "learned")).lower()
    return None if r in ("learned", "rolling", "") else parse_when(win["reset"], at)


def _used(data: dict, lane: str, window: str, after: dt.datetime, cfg: dict) -> float:
    total = 0.0
    for e in data["events"]:
        if e.get("lane") != lane or e.get("type") != "run":
            continue
        try:
            if dt.datetime.fromisoformat(e["at"]) <= after:
                continue
        except (KeyError, ValueError):
            continue
        cost = run_cost(cfg, lane, window, e.get("weight", "light"), e.get("model"))
        total += cost or 0.0
    return total


def window_estimate(cfg: dict, data: dict, lane: str, name: str, at: dt.datetime) -> dict:
    """{"left": percent or None, "resets": datetime or None, "source": where it came from}."""
    win = windows(cfg, lane).get(name) or {}
    hours = float(win.get("hours") or 0)
    r = ((data["readings"].get(lane) or {}).get(name)) or None
    if not r:
        return {"left": None, "resets": _scheduled(win, at), "source": "unknown"}
    seen = dt.datetime.fromisoformat(r["at"])
    resets = dt.datetime.fromisoformat(r["resets"]) if r.get("resets") else _scheduled(win, seen)
    if resets and at >= resets:  # the window rolled over since the reading
        last = resets
        if _repeats(win):
            while last + dt.timedelta(hours=hours) <= at:
                last += dt.timedelta(hours=hours)
            nxt = last + dt.timedelta(hours=hours)
        else:
            nxt = _scheduled(win, at)
        left = 100.0 - _used(data, lane, name, last, cfg)
        return {"left": max(0.0, left), "resets": nxt, "source": f"reset since {r.get('source', '?')}"}
    left = float(r["left"]) - _used(data, lane, name, seen, cfg)
    return {"left": max(0.0, left), "resets": resets, "source": r.get("source", "?")}


def fmt_hours(delta: dt.timedelta) -> str:
    h = delta.total_seconds() / 3600
    return f"{h * 60:.0f}m" if h < 1 else f"{h:.0f}h" if h <= 72 else f"{h / 24:.1f}d"


def estimate(cfg: dict, data: dict, lane: str, at: dt.datetime, limited_until: dt.datetime | None = None) -> dict:
    """The lane now: every window's estimate, the tightest one, and the mode with its reason."""
    wins = {n: window_estimate(cfg, data, lane, n, at) for n in windows(cfg, lane)}
    known = {n: w for n, w in wins.items() if w["left"] is not None}
    tight = min(known, key=lambda n: known[n]["left"]) if known else None
    out = {"lane": lane, "windows": wins, "window": tight, "left": known[tight]["left"] if tight else None,
           "resets": known[tight]["resets"] if tight else None, "mode": "spread", "why": ""}
    spend_h = float(cfg.get("allowance_spend_down_hours", 6))
    low = float(cfg.get("allowance_conserve_below_pct", 25))

    def hrs(w):
        return None if not w["resets"] else (w["resets"] - at).total_seconds() / 3600

    def say(n, w):
        r = f", reset in {fmt_hours(w['resets'] - at)}" if w["resets"] else ", reset unknown"
        return f"{w['left']:.0f}% {n} left{r}"

    if limited_until and limited_until > at:
        out.update(mode="held", why=f"{lane} limited until {limited_until:%a %H:%M}")
        return out
    far_low = [n for n, w in known.items() if w["left"] < low and (hrs(w) is None or hrs(w) > spend_h)]
    if far_low:
        n = min(far_low, key=lambda n: known[n]["left"])
        out.update(mode="conserve", window=n, left=known[n]["left"], resets=known[n]["resets"],
                   why=f"{lane} held: {say(n, known[n])}")
        return out
    near = [n for n, w in known.items() if w["left"] > 0 and hrs(w) is not None and hrs(w) <= spend_h]
    if near:
        n = min(near, key=lambda n: (hrs(known[n]), -known[n]["left"]))
        out.update(mode="spend", spend_window=n, spend_resets=known[n]["resets"],
                   why=f"{lane} spending down: {say(n, known[n])} (use it or lose it)")
        return out
    out["why"] = f"{lane}: {say(tight, known[tight])}" if tight else f"{lane}: allowance unknown"
    return out


def hold(cfg: dict, data: dict, lane: str, at: dt.datetime, *, priority: str = "normal",
         weight: str = "light", model: str | None = None, limited_until: dt.datetime | None = None) -> str | None:
    """Why this lane should not take this task now, or None if it may."""
    est = estimate(cfg, data, lane, at, limited_until)
    if est["mode"] == "held":
        return est["why"]
    for n, w in est["windows"].items():
        cost = run_cost(cfg, lane, n, weight, model)
        if w["left"] is not None and cost is not None and w["left"] < cost:
            r = f", reset in {fmt_hours(w['resets'] - at)}" if w["resets"] else ""
            return f"{lane} held: a {weight} run needs ~{cost:.0f}% of {n}, {w['left']:.0f}% left{r}"
    if est["mode"] == "conserve":
        reserve = float(cfg.get("allowance_reserve_pct", 5))
        cost = run_cost(cfg, lane, est["window"], weight, model) or 0
        if priority == "urgent" and est["left"] - cost >= reserve:
            return None
        return est["why"] + (f" (reserve {reserve:.0f}% kept for urgent tasks)" if priority == "urgent" else "")
    return None


def choose(cfg: dict, data: dict, candidates: list, at: dt.datetime, *, priority: str = "normal",
           weight: str = "light", model_of=None, limited: dict | None = None,
           recent: dict | None = None) -> tuple[str | None, str]:
    """(lane, why) for a task the planner places. Lanes that may not take it are dropped; a
    spend-down lane wins (soonest reset, then most left, then candidate order); else the one
    with the fewest recent runs (candidate order breaks a tie)."""
    limited, recent, model_of = limited or {}, recent or {}, model_of or (lambda _l: None)
    ok, why_not = [], []
    for lane in candidates:
        h = hold(cfg, data, lane, at, priority=priority, weight=weight, model=model_of(lane),
                 limited_until=limited.get(lane))
        (why_not.append(h) if h else ok.append(lane))
    if not ok:
        return None, "no lane may take it: " + "; ".join(why_not) if why_not else "no candidate lanes"
    ests = {l: estimate(cfg, data, l, at, limited.get(l)) for l in ok}
    spend = [l for l in ok if ests[l]["mode"] == "spend"]
    if spend:
        lane = min(spend, key=lambda l: (ests[l]["spend_resets"], -(ests[l]["left"] or 0), ok.index(l)))
        return lane, ests[lane]["why"]
    lane = min(ok, key=lambda l: (recent.get(l, 0), ok.index(l)))
    tail = f"; {ests[lane]['why']}" if ests[lane]["left"] is not None else ""
    return lane, (f"{lane}: fewest recent runs (" + ", ".join(f"{l} {recent.get(l, 0)}" for l in ok) + ")" + tail)


# ---------------------------------------------------------------- evidence

def _reading(data: dict, lane: str, window: str, left: float, resets: dt.datetime | None,
             at: dt.datetime, source: str) -> None:
    old = (data["readings"].get(lane) or {}).get(window)
    if old and old.get("at", "") > at.isoformat():
        return  # an older log never overwrites a newer reading
    data["readings"].setdefault(lane, {})[window] = {
        "left": round(float(left), 1), "resets": resets.isoformat() if resets else None,
        "at": at.isoformat(), "source": source}


def observations(text: str) -> list[dict]:
    """Readings a lane log carries about itself: Claude's rate_limit_event, every window's
    utilization and resetsAt (unifiedWindows), the last one per window."""
    out: dict = {}
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("{") or "rate_limit_event" not in s:
            continue
        try:
            ev = json.loads(s)
        except ValueError:
            continue
        info = (ev.get("rate_limit_info") or {}) if isinstance(ev, dict) else {}
        for name, w in (info.get("unifiedWindows") or {}).items():
            if not isinstance(w, dict) or not isinstance(w.get("utilization"), (int, float)):
                continue
            out[name] = {"window": name, "left": max(0.0, 100.0 * (1 - float(w["utilization"]))),
                         "resets": dt.datetime.fromtimestamp(int(w["resetsAt"])) if w.get("resetsAt") else None}
    return list(out.values())


def limit_window(cfg: dict, lane: str, message: str, until: dt.datetime, at: dt.datetime) -> str | None:
    """Which window a limit hit belongs to: one named in the message (Claude's rateLimitType,
    e.g. "five_hour"), else the shortest window at least as long as the wait, else the longest."""
    wins = windows(cfg, lane)
    if not wins:
        return None
    for name in wins:
        if re.search(rf"\b{re.escape(name)}\b", message or "", re.I):
            return name
    wait_h = (until - at).total_seconds() / 3600
    by_len = sorted(wins, key=lambda n: float(wins[n].get("hours") or 0))
    return next((n for n in by_len if float(wins[n].get("hours") or 0) >= wait_h), by_len[-1])


def record_run(path: Path, cfg: dict, lane: str, at: dt.datetime, *, slug: str = "", weight: str = "light",
               model: str | None = None, tokens: dict | None = None, cost: float | None = None,
               turns: int | None = None, seconds: int = 0, log_text: str = "") -> dict:
    """One run: its usage as an event, and any readings its own log carried."""
    def fn(data):
        data["events"].append({"at": at.isoformat(), "type": "run", "lane": lane, "slug": slug, "weight": weight,
                               "model": model, "tokens": tokens, "cost_usd": cost, "turns": turns,
                               "seconds": seconds})
        for o in observations(log_text):
            _reading(data, lane, o["window"], o["left"], o["resets"], at, "lane log")
    return _update(path, cfg, fn)


def record_limit(path: Path, cfg: dict, lane: str, at: dt.datetime, until: dt.datetime, message: str) -> dict:
    """A LIMITED: the window it names (or fits) is at 0 until `until`. That reset is learned."""
    def fn(data):
        win = limit_window(cfg, lane, message, until, at)
        data["events"].append({"at": at.isoformat(), "type": "limited", "lane": lane, "window": win,
                               "until": until.isoformat(), "message": (message or "")[:300]})
        if win:
            _reading(data, lane, win, 0, until, at, "limit error")
    return _update(path, cfg, fn)


def record_set(path: Path, cfg: dict, lane: str, at: dt.datetime, left: float, resets: dt.datetime | None,
               window: str | None = None) -> tuple[dict, str]:
    """What Chuen states. Default window: the lane's longest (a weekly one, usually)."""
    wins = windows(cfg, lane)
    win = window or (max(wins, key=lambda n: float(wins[n].get("hours") or 0)) if wins else "weekly")

    def fn(data):
        data["events"].append({"at": at.isoformat(), "type": "set", "lane": lane, "window": win,
                               "left": left, "resets": resets.isoformat() if resets else None})
        _reading(data, lane, win, left, resets, at, "Chuen")
    return _update(path, cfg, fn), win


def learn_logs(path: Path, cfg: dict, logs: list, parse_until, lane_of) -> int:
    """Readings from old lane logs (time = the file's mtime): Claude's own windows, and the
    'try again at' time of a limit message (parse_until(text, at) -> datetime or None)."""
    n = 0

    def fn(data):
        nonlocal n
        for p in logs:
            try:
                text = Path(p).read_text(encoding="utf-8", errors="replace")
                at = dt.datetime.fromtimestamp(Path(p).stat().st_mtime).replace(microsecond=0)
            except OSError:
                continue
            lane = lane_of(p, text)
            if not lane:
                continue
            for o in observations(text):
                _reading(data, lane, o["window"], o["left"], o["resets"], at, "lane log")
                n += 1
            for line in reversed(text.splitlines()[-200:]):
                if not re.search(cfg.get("allowance_learn_pattern", "usage limit|try again"), line, re.I):
                    continue
                until = parse_until(line, at)
                if until:
                    win = limit_window(cfg, lane, line, until, at)
                    if win:
                        _reading(data, lane, win, 0, until, at, "limit error")
                        n += 1
                break
    _update(path, cfg, fn)
    return n


# ---------------------------------------------------------------- CLI

def describe(est: dict, at: dt.datetime) -> str:
    parts = []
    for n, w in est["windows"].items():
        left = "?" if w["left"] is None else f"{w['left']:.0f}%"
        r = f"resets {w['resets']:%a %d %b %H:%M} (in {fmt_hours(w['resets'] - at)})" if w["resets"] else "reset unknown"
        parts.append(f"{n} {left}, {r} [{w['source']}]")
    return "; ".join(parts) or "no windows configured"


def rows(cfg: dict, data: dict, lanes: list, at: dt.datetime, limited: dict | None = None) -> list[dict]:
    """One dict per lane for status/STATE NOW: left, window, resets, mode, why, windows."""
    out = []
    for lane in lanes:
        e = estimate(cfg, data, lane, at, (limited or {}).get(lane))
        out.append({"lane": lane, "mode": e["mode"], "why": e["why"], "window": e["window"],
                    "left_pct": None if e["left"] is None else round(e["left"], 1),
                    "resets": e["resets"].isoformat() if e["resets"] else None,
                    "windows": {n: {"left_pct": None if w["left"] is None else round(w["left"], 1),
                                    "resets": w["resets"].isoformat() if w["resets"] else None,
                                    "source": w["source"]} for n, w in e["windows"].items()},
                    "text": describe(e, at)})
    return out


MODE_WORDS = {"held": "held (limited)", "conserve": "conserved", "spend": "spending down", "spread": "spread"}


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import runner as R
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="state a lane's allowance, e.g. set codex --left 10%% --resets 2026-11-11T12:00")
    s.add_argument("lane")
    s.add_argument("--left", required=True, help="percent left, e.g. 10%% or 10")
    s.add_argument("--resets", help="when that window resets: 2026-11-11T12:00 or 'Wed 12:00' (local time)")
    s.add_argument("--window", help="which window (default: the lane's longest, see config allowance)")
    sh = sub.add_parser("show", help="every lane's estimated allowance now")
    sh.add_argument("--json", action="store_true")
    sub.add_parser("learn", help="read readings and reset times from the lane logs in .review/reports")
    a = ap.parse_args(argv)
    path, at = R.allowance_path(), R.now()
    if a.cmd == "set":
        try:
            left = float(a.left.strip().rstrip("%"))
        except ValueError:
            ap.error(f"--left {a.left!r} is not a percentage")
        if not 0 <= left <= 100:
            ap.error("--left must be 0-100%")
        resets = parse_when(a.resets, at) if a.resets else None
        if a.resets and not resets:
            ap.error(f"--resets {a.resets!r}: use 2026-11-11T12:00 or 'Wed 12:00'")
        wins = windows(R.C, a.lane)
        if a.window and wins and a.window not in wins:
            ap.error(f"--window {a.window!r} is not one of {', '.join(wins)} (config allowance.{a.lane}.windows)")
        data, win = record_set(path, R.C, a.lane, at, left, resets, a.window)
        print(f"{a.lane} {win}: {left:.0f}% left" + (f", resets {resets:%a %d %b %H:%M}" if resets else ""))
        print(f"now: {estimate(R.C, data, a.lane, at)['why']}")
        return 0
    if a.cmd == "learn":
        logs = sorted(R.REPORTS.glob("*.lane.log"), key=lambda p: p.stat().st_mtime)
        n = learn_logs(path, R.C, logs, R.parse_limit_until, R.lane_of_log)
        print(f"{n} reading(s) learned from {len(logs)} lane log(s)")
        return 0
    rs = R.allowance_rows(R.load_state())
    if a.json:
        print(json.dumps(rs, indent=2))
    else:
        for r in rs:
            print(f"{r['lane']}: {MODE_WORDS.get(r['mode'], r['mode'])} -- {r['why']}\n  {r['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
