"""Is the laptop on AC or battery? The runner, not Task Scheduler, decides what to do about it
(2 Nov 2026: Windows killed runner ticks on unplug and left their claude -p lanes orphaned).

    power_status() -> {"ac": bool|None, "battery_percent": int|None}

From kernel32 GetSystemPowerStatus via ctypes (no new dependency). None means unknown;
callers treat unknown as AC (a desktop with no battery, or a non-Windows test box).
ORCHESTRA_POWER overrides the reading: "ac", "battery", "battery:42" or "unknown" (tests,
or forcing a run by hand).
"""
from __future__ import annotations

import ctypes
import os


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
