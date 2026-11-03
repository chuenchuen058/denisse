"""
Reminders, task-list and timer data: wording, durations, colours, sizes and
file names for tasks.py, reminder_card.py, timers.py and timer_ui.py.

Why (AGENTS.md Rule 0, 2026-11-03): those modules had every sentence,
colour, delay and limit inline. The values now live in reminders.json beside
this file; the shipped values are in reminders.defaults.json.

Loaded once per process (reload() re-reads, for tests). Each value in
reminders.json is checked against its shipped default -- same type, numbers
positive where the default is, colours as #rrggbb, templates
using only the {fields} the default uses (and {user}, always). A missing or bad value logs one
clear line and the default is used instead: a typo in the data file must
never stop the card or a timer.

DENISSE_REMINDERS points at another data file (tests, A/Bs).
"""

import copy
import json
import logging
import os
import re
import string
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(ROOT, "reminders.json")
DEFAULTS_FILE = os.path.join(ROOT, "reminders.defaults.json")

log = logging.getLogger("denisse.reminders")

_lock = threading.Lock()
_cache = None

_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")


def path() -> str:
    return os.environ.get("DENISSE_REMINDERS") or DEFAULT_FILE


def _read(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _fields(template: str) -> set:
    return {name.split(".")[0].split("[")[0]
            for _, name, _, _ in string.Formatter().parse(template) if name}


def _bad(where, why, default):
    log.warning("reminders.json: %s %s -- using the default %r", where, why, default)
    return copy.deepcopy(default)


def _check(where, value, default):
    """value if it fits the shape of default, else default (logged)."""
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return _bad(where, "should be an object", default)
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
        if not default:
            return value
        if not value and isinstance(default[0], dict):
            return _bad(where, "should not be empty", default)
        out = []
        for i, v in enumerate(value):
            proto = default[min(i, len(default) - 1)] if isinstance(default[0], dict) else default[0]
            if isinstance(proto, dict):
                if not isinstance(v, dict) or any(k not in v for k in proto if not k.startswith("_")):
                    return _bad(f"{where}[{i}]", f"needs the keys {sorted(proto)}", default)
                checked = {k: _check(f"{where}[{i}].{k}", v[k], proto[k]) for k in proto}
                if any(checked[k] is not v[k] and checked[k] != v[k] for k in proto):
                    return _bad(f"{where}[{i}]", "has a bad value", default)
                out.append(checked)
            elif type(v) is not type(proto):
                return _bad(f"{where}[{i}]", f"should be a {type(proto).__name__}", default)
            else:
                out.append(v)
        return out
    if isinstance(default, bool):
        return value if isinstance(value, bool) else _bad(where, "should be true or false", default)
    if isinstance(default, (int, float)):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        if isinstance(default, int) and not isinstance(value, int):
            ok = False
        if not ok:
            return _bad(where, f"should be a number, got {value!r}", default)
        if default > 0 and value <= 0:
            return _bad(where, f"should be above 0, got {value!r}", default)
        return value
    if isinstance(default, str):
        if not isinstance(value, str):
            return _bad(where, f"should be text, got {value!r}", default)
        if _COLOUR.match(default) and not _COLOUR.match(value):
            return _bad(where, f"should be a #rrggbb colour, got {value!r}", default)
        try:
            extra = _fields(value) - _fields(default) - {"user"}
        except ValueError as exc:
            return _bad(where, f"isn't a valid template ({exc})", default)
        if extra:
            return _bad(where, f"uses unknown fields {sorted(extra)}", default)
        return value
    return value


def _extra_checks(data, defaults):
    names = [p["name"] for p in data["priorities"]]
    if len(set(names)) != len(names):
        data["priorities"] = _bad("priorities", "has a repeated name", defaults["priorities"])
        names = [p["name"] for p in data["priorities"]]
    if data["tasks"]["default_priority"] not in names:
        fallback = defaults["tasks"]["default_priority"]
        data["tasks"]["default_priority"] = _bad(
            "tasks.default_priority", f"isn't one of {names}",
            fallback if fallback in names else names[0])
    hours = [g["until_hour"] for g in data["card"]["greetings"]]
    if hours != sorted(hours) or hours[-1] < 24:
        data["card"]["greetings"] = _bad(
            "card.greetings", "should run in order of until_hour and end at 24",
            defaults["card"]["greetings"])
    if not 0 < data["card"]["layout"]["list_max_frac"] <= 1:
        data["card"]["layout"]["list_max_frac"] = _bad(
            "card.layout.list_max_frac", "should be between 0 and 1",
            defaults["card"]["layout"]["list_max_frac"])
    return data


def _build():
    try:
        defaults = _read(DEFAULTS_FILE)
    except (OSError, ValueError) as exc:
        log.error("reminders.defaults.json unreadable (%s) -- using reminders.json unchecked", exc)
        return _read(path())
    p = path()
    try:
        live = _read(p)
    except (OSError, ValueError) as exc:
        log.warning("reminders.json unreadable at %s (%s) -- using the shipped defaults", p, exc)
        live = defaults
    return _extra_checks(_check("reminders", live, defaults), defaults)


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


def user() -> str:
    """Whose assistant this is, for {user} in templates: the first word of
    settings.user_name(), since the card and timers address him by his
    given name ("Morning, Chuen"), not his full one."""
    try:
        import settings

        return (settings.user_name().split() or [""])[0]
    except Exception as exc:
        log.warning("no user name for reminder wording (%s)", exc)
        return ""


def fill(template: str, **fields) -> str:
    """Fill a template from this file; {user} is filled in when it's used."""
    if "user" in _fields(template) and "user" not in fields:
        fields["user"] = user()
    return template.format(**fields)


def say(dotted: str, **fields) -> str:
    """fill() the template at `dotted`."""
    return fill(get(dotted), **fields)


def priorities() -> list:
    """Priority names, in display order."""
    return [p["name"] for p in get("priorities")]


def priority(name: str) -> dict:
    for p in get("priorities"):
        if p["name"] == name:
            return p
    return None
