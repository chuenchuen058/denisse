"""Is the laptop on AC or battery? The runner, not Task Scheduler, decides what to do about it
(2 Nov 2026: Windows killed runner ticks on unplug and left their claude -p lanes orphaned).

    power_status() -> {"ac": bool|None, "battery_percent": int|None}

From kernel32 GetSystemPowerStatus via ctypes (no new dependency). None means unknown;
callers treat unknown as AC (a desktop with no battery, or a non-Windows test box).
ORCHESTRA_POWER overrides the reading: "ac", "battery", "battery:42" or "unknown" (tests,
or forcing a run by hand).

One-task override (Chuen, 9 Nov 2026: "so that I can override it for just one task if I said so"):

    .venv\\Scripts\\python.exe orchestra\\power.py allow-once [--task <slug>] [--lane <lane>] [--why ...]
    .venv\\Scripts\\python.exe orchestra\\power.py status

writes a grant file under .review/ that lets the runner start exactly one new lane while on
battery; the runner deletes it in the tick that uses it. Never at or under battery_floor_percent,
void after power_override_minutes unused, and config.json is never edited. File names, minutes
and every line of wording come from config (power_override_*).
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import getpass
import json
import os
import sys
from pathlib import Path


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte),        # 0 offline, 1 online, 255 unknown
                ("BatteryFlag", ctypes.c_ubyte),         # 128 no system battery, 255 unknown
                ("BatteryLifePercent", ctypes.c_ubyte),  # 0-100, 255 unknown
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong)]


UNKNOWN = {"ac": None, "battery_percent": None}


def parse(sps) -> dict:
    """A SYSTEM_POWER_STATUS (or anything with the same fields) -> power_status()'s dict."""
    ac = {0: False, 1: True}.get(int(sps.ACLineStatus))
    flag, pct = int(sps.BatteryFlag), int(sps.BatteryLifePercent)
    no_battery = flag != 255 and bool(flag & 128)
    return {"ac": ac, "battery_percent": None if no_battery or pct > 100 else pct}


def _override(text: str) -> dict:
    s = text.strip().lower()
    if s == "ac":
        return {"ac": True, "battery_percent": None}
    if s.startswith("battery"):
        _, _, n = s.partition(":")
        return {"ac": False, "battery_percent": int(n) if n.strip().isdigit() else None}
    return dict(UNKNOWN)


def power_status() -> dict:
    env = os.environ.get("ORCHESTRA_POWER")
    if env:
        return _override(env)
    if os.name != "nt":
        return dict(UNKNOWN)
    try:
        sps = SYSTEM_POWER_STATUS()
        if not ctypes.WinDLL("kernel32").GetSystemPowerStatus(ctypes.byref(sps)):
            return dict(UNKNOWN)
        return parse(sps)
    except (OSError, AttributeError):
        return dict(UNKNOWN)


def on_battery(status: dict) -> bool:
    return status.get("ac") is False  # None (unknown) counts as AC


def dispatch_block(status: dict, cfg: dict) -> str | None:
    """Why no new lane may start now, or None. Running lanes are never stopped for power:
    the floor only ends new dispatch, even when dispatch_on_battery is on."""
    if not on_battery(status):
        return None
    pct = status.get("battery_percent")
    floor = int(cfg.get("battery_floor_percent", 30))
    if not cfg.get("dispatch_on_battery", False):
        return "dispatch_on_battery is off"
    if pct is not None and pct <= floor:
        return f"battery at {pct}%, at or under the {floor}% floor"
    return None


def describe(status: dict, cfg: dict) -> str:
    """'AC' or 'battery 87% (dispatch paused)' for STATE NOW and status.py."""
    if not on_battery(status):
        return "AC" if status.get("ac") else "AC (assumed: power state unknown)"
    pct = status.get("battery_percent")
    s = f"battery {pct}%" if pct is not None else "battery"
    return s + (" (dispatch paused)" if dispatch_block(status, cfg) else " (dispatching)")


# ---------------------------------------------------------------- one-task override

def text(cfg: dict, key: str, **kw) -> str:
    return str((cfg.get("power_override_text") or {}).get(key, key)).format(**kw)


def _floor(cfg: dict) -> int:
    return int(cfg.get("battery_floor_percent", 30))


def _at_floor(status: dict, cfg: dict) -> bool:
    pct = status.get("battery_percent")
    return pct is not None and pct <= _floor(cfg)


def grant_path(review: Path, cfg: dict) -> Path:
    return Path(review) / cfg["power_override_file"]


def used_path(review: Path, cfg: dict) -> Path:
    return Path(review) / cfg["power_override_used_file"]


def _read(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _expired(grant: dict, now: dt.datetime) -> bool:
    try:
        return now >= dt.datetime.fromisoformat(grant["expires"])
    except (KeyError, TypeError, ValueError):
        return True  # unreadable expiry: never honoured


def read_grant(review: Path, cfg: dict) -> dict | None:
    """The grant file's content, expired or not, or None."""
    return _read(grant_path(review, cfg))


def usable_grant(review: Path, cfg: dict, status: dict, now: dt.datetime) -> dict | None:
    """A grant the runner may use right now: present, unexpired, on battery, above the floor."""
    if not on_battery(status) or _at_floor(status, cfg):
        return None
    g = read_grant(review, cfg)
    return None if g is None or _expired(g, now) else g


def is_expired(grant: dict, now: dt.datetime) -> bool:
    return _expired(grant, now)


def allow_once(review: Path, cfg: dict, status: dict, now: dt.datetime, task: str | None = None,
               lane: str | None = None, who: str = "", why: str = "") -> tuple[bool, str]:
    """Write the grant if there is a battery block to override: (written?, message)."""
    if _at_floor(status, cfg) and on_battery(status):
        return False, text(cfg, "refused_floor", pct=status.get("battery_percent"), floor=_floor(cfg))
    if not dispatch_block(status, cfg):
        return False, text(cfg, "not_needed", power=describe(status, cfg))
    minutes = int(cfg["power_override_minutes"])
    grant = {"who": who, "why": why, "created": now.isoformat(timespec="seconds"),
             "expires": (now + dt.timedelta(minutes=minutes)).isoformat(timespec="seconds"),
             "task": task or None, "lane": lane or None}
    p = grant_path(review, cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(grant, indent=1), encoding="utf-8")
    return True, text(cfg, "granted", target=target(cfg, grant), minutes=minutes, expires=_hm(grant["expires"]))


def drop_grant(review: Path, cfg: dict) -> None:
    try:
        grant_path(review, cfg).unlink()
    except FileNotFoundError:
        pass


def consume(review: Path, cfg: dict, grant: dict, slug: str, pct, now: dt.datetime) -> str:
    """Delete the grant and record its use; returns the runner's log line."""
    drop_grant(review, cfg)
    used = {**grant, "used": now.isoformat(timespec="seconds"), "used_for": slug, "pct": pct}
    used_path(review, cfg).write_text(json.dumps(used, indent=1), encoding="utf-8")
    return text(cfg, "used_log", slug=slug, pct="?" if pct is None else pct)


def _hm(iso) -> str:
    try:
        return dt.datetime.fromisoformat(iso).strftime("%H:%M")
    except (TypeError, ValueError):
        return "?"


def target(cfg: dict, grant: dict) -> str:
    parts = []
    if grant.get("task"):
        parts.append(text(cfg, "for_task", task=grant["task"]))
    if grant.get("lane"):
        parts.append(text(cfg, "on_lane", lane=grant["lane"]))
    return " ".join(parts) or text(cfg, "any_task")


def override_line(review: Path, cfg: dict, now: dt.datetime) -> str | None:
    """One line on a pending (or expired, not yet cleared) grant, or on one used within
    power_override_minutes; else None."""
    g = read_grant(review, cfg)
    if g is not None:
        key = "expired_line" if _expired(g, now) else "pending_line"
        return text(cfg, key, target=target(cfg, g), expires=_hm(g.get("expires")), who=g.get("who") or "?")
    u = _read(used_path(review, cfg))
    try:
        recent = u is not None and (now - dt.datetime.fromisoformat(u["used"])) <= dt.timedelta(
            minutes=int(cfg["power_override_minutes"]))
    except (KeyError, TypeError, ValueError):
        recent = False
    if recent:
        return text(cfg, "used_line", slug=u.get("used_for"), at=_hm(u["used"]),
                     pct="?" if u.get("pct") is None else u["pct"])
    return None


def file_config(repo: Path) -> dict:
    """config_defaults.json then the repo's config.json, `_` keys dropped: for readers that don't
    import the runner (watch.py). The runner's own C is the validated version."""
    here = Path(__file__).resolve().parent
    out: dict = {}
    for p in (here / "config_defaults.json", Path(repo) / "orchestra" / "config.json"):
        out.update({k: v for k, v in (_read(p) or {}).items() if not k.startswith("_")})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Power state, and a one-task override for battery.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="the power reading and any pending override")
    a = sub.add_parser("allow-once", help="let exactly one new lane start while on battery")
    a.add_argument("--task", help="only this task slug")
    a.add_argument("--lane", help="only a task on this lane")
    a.add_argument("--who", default=None, help="who granted it (default: this user)")
    a.add_argument("--why", default="", help="why, for the record")
    args = ap.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import runner as R  # the validated config and the real .review/ (honours ORCHESTRA_REPO)
    st, now = power_status(), R.now()
    if args.cmd == "status":
        print(text(R.C, "status_power", power=describe(st, R.C)))
        print(override_line(R.REVIEW, R.C, now) or text(R.C, "none_line"))
        return 0
    ok, msg = allow_once(R.REVIEW, R.C, st, now, task=args.task, lane=args.lane,
                         who=args.who if args.who is not None else getpass.getuser(), why=args.why)
    print(msg)
    if ok:
        R.log(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
