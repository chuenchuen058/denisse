"""
ui_theme.json: colours, sizes, fonts, timings and labels for Denisse's own
windows -- the floating orb (floating_orb.py) and its hover dock
(orb_controls.py).

Why (AGENTS.md Rule 0, 2026-11-03): both modules had every colour, size,
animation speed and label inline. The values now live in ui_theme.json beside
this file; the shipped values, documented key by key, are in
ui_theme.defaults.json.

Loaded once per process (reload() re-reads, for tests). Every value in
ui_theme.json is checked against its shipped default -- same type, numbers
positive where the default is, colours as #rrggbb, templates using only the
{fields} the default uses -- plus a few cross-checks (motion kinds, dock
actions and icons, alpha <= 255). A missing or bad value logs one clear line
and the default is used instead: a typo in the theme must never stop the orb.

DENISSE_UI_THEME points at another theme file (tests, A/Bs).
"""

import copy
import json
import logging
import os
import re
import string
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(ROOT, "ui_theme.json")
DEFAULTS_FILE = os.path.join(ROOT, "ui_theme.defaults.json")

log = logging.getLogger("denisse.ui_theme")

# What the code can draw / do -- names the data picks from, not values.
MOTION_KINDS = ("shake", "bounce", "orbit", "lift", "bob", "still")
DOCK_ACTIONS = ("chat", "voice", "reminders")
DOCK_ICONS = ("chat", "mic", "bell")
ALPHA_MAX = 255

_lock = threading.Lock()
_cache = None

_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")


def path() -> str:
    return os.environ.get("DENISSE_UI_THEME") or DEFAULT_FILE


def _read(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _fields(template: str) -> set:
    return {name.split(".")[0].split("[")[0]
            for _, name, _, _ in string.Formatter().parse(template) if name}


def _bad(where, why, default):
    log.warning("ui_theme.json: %s %s -- using the default %r", where, why, default)
    return copy.deepcopy(default)


def _check(where, value, default):
    """value if it fits the shape of default, else default (logged)."""
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return _bad(where, "should be an object", default)
        for k in value:
            if not k.startswith("_") and k not in default:
                log.warning("ui_theme.json: %s.%s is not a known key -- ignored", where, k)
        out = {}
        for k, d in default.items():
            if k.startswith("_"):
                continue
            if k not in value:
                out[k] = _bad(f"{where}.{k}", "is missing", d)
            else:
                out[k] = _check(f"{where}.{k}", value[k], d)
        return out
    if isinstance(default, list):
        if not isinstance(value, list):
            return _bad(where, "should be a list", default)
        if not value:
            return _bad(where, "should not be empty", default)
        out = []
        for i, v in enumerate(value):
            proto = default[min(i, len(default) - 1)]
            if isinstance(proto, dict):
                if not isinstance(v, dict) or any(k not in v for k in proto if not k.startswith("_")):
                    return _bad(f"{where}[{i}]", f"needs the keys {sorted(k for k in proto if not k.startswith('_'))}", default)
                checked = {k: _check(f"{where}[{i}].{k}", v[k], proto[k]) for k in proto if not k.startswith("_")}
                if any(checked[k] != v[k] for k in checked):
                    return _bad(f"{where}[{i}]", "has a bad value", default)
                out.append(checked)
            else:
                out.append(_check(f"{where}[{i}]", v, proto))
        return out
    if isinstance(default, bool):
        return value if isinstance(value, bool) else _bad(where, "should be true or false", default)
    if isinstance(default, (int, float)):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        if isinstance(default, int) and not isinstance(value, int):
            ok = False
        if not ok:
            return _bad(where, f"should be a{'' if isinstance(default, float) else ' whole'} number, got {value!r}", default)
        if default > 0 and value <= 0:
            return _bad(where, f"should be above 0, got {value!r}", default)
        if default >= 0 and value < 0:
            return _bad(where, f"should not be negative, got {value!r}", default)
        return value
    if isinstance(default, str):
        if not isinstance(value, str):
            return _bad(where, f"should be text, got {value!r}", default)
        if _COLOUR.match(default) and not _COLOUR.match(value):
            return _bad(where, f"should be a #rrggbb colour, got {value!r}", default)
        try:
            extra = _fields(value) - _fields(default)
        except ValueError as exc:
            return _bad(where, f"isn't a valid template ({exc})", default)
        if extra:
            return _bad(where, f"uses unknown fields {sorted(extra)}", default)
        return value
    return value


def _fix(data, defaults, dotted, why):
    """Replace data[dotted] with its default, logged."""
    *head, last = dotted.split(".")
    node, dnode = data, defaults
    for part in head:
        node, dnode = node[part], dnode[part]
    node[last] = _bad(dotted, why, dnode[last])


def _extra_checks(data, defaults):
    orb, anim, dock = data["orb"], data["animation"], data["dock"]
    for key in ("alpha_snap_threshold", "flatten_alpha"):
        if orb[key] > ALPHA_MAX:
            _fix(data, defaults, f"orb.{key}", f"should be at most {ALPHA_MAX}")
    if orb["core"]["highlight_alpha"] > ALPHA_MAX:
        _fix(data, defaults, "orb.core.highlight_alpha", f"should be at most {ALPHA_MAX}")
    if orb["core"]["radius_rest"] + orb["core"]["radius_swing"] > 1:
        _fix(data, defaults, "orb.core.radius_rest", "plus radius_swing should be at most 1")
        _fix(data, defaults, "orb.core.radius_swing", "plus radius_rest should be at most 1")
    if orb["glow"]["alpha_rest"] + orb["glow"]["alpha_swing"] > ALPHA_MAX:
        _fix(data, defaults, "orb.glow.alpha_rest", f"plus alpha_swing should be at most {ALPHA_MAX}")
        _fix(data, defaults, "orb.glow.alpha_swing", f"plus alpha_rest should be at most {ALPHA_MAX}")
    if any(a["colour"] not in data["palette"] for a in orb["ring"]["arcs"]):
        _fix(data, defaults, "orb.ring.arcs", f"should name palette colours {sorted(data['palette'])}")
    if anim["ring_deg_per_tick"] > 360:
        _fix(data, defaults, "animation.ring_deg_per_tick", "should be at most 360")
    for mood, m in anim["motion"].items():
        if m["kind"] not in MOTION_KINDS:
            _fix(data, defaults, f"animation.motion.{mood}", f"kind should be one of {list(MOTION_KINDS)}")
    actions = [b["action"] for b in dock["buttons"]]
    if (len(set(actions)) != len(actions)
            or any(a not in DOCK_ACTIONS for a in actions)
            or any(b["icon"] not in DOCK_ICONS for b in dock["buttons"])):
        _fix(data, defaults, "dock.buttons",
             f"should use each action of {list(DOCK_ACTIONS)} at most once and icons from {list(DOCK_ICONS)}")
    if 2 * dock["layout"]["button_top"] >= dock["layout"]["height"]:
        _fix(data, defaults, "dock.layout.button_top", "should be under half of dock.layout.height")
    return data


def _build():
    try:
        defaults = _read(DEFAULTS_FILE)
    except (OSError, ValueError) as exc:
        log.error("ui_theme.defaults.json unreadable (%s) -- using ui_theme.json unchecked", exc)
        return _read(path())
    p = path()
    try:
        live = _read(p)
    except (OSError, ValueError) as exc:
        log.warning("ui_theme.json unreadable at %s (%s) -- using the shipped defaults", p, exc)
        live = defaults
    return _extra_checks(_check("ui_theme", live, defaults), defaults)


def load() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _build()
        return _cache


def reload() -> dict:
    global _cache
    with _lock:
        _cache = None
    return load()


def get(dotted: str):
    node = load()
    for part in dotted.split("."):
        node = node[part]
    return node


def rgb(colour: str) -> tuple:
    """'#rrggbb' -> (r, g, b), for PIL fills that need a tuple."""
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


def colour(name: str) -> str:
    """A palette colour by name."""
    return get("palette")[name]
