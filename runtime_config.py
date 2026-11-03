"""Intervals, limits, wording and word lists for standing_instructions.py,
conversation.py and runtime_status.py (AGENTS.md Rule 0, 2026-11-03).

The documented defaults are in runtime.defaults.json beside this file. To
change one, put the same key, same nesting, in config.json under
settings.runtime (read through settings.get, so DENISSE_CONFIG points at
another copy for tests):

    "settings": {"runtime": {"runtime_status": {"write_every_seconds": 20}}}

    runtime_config.get("runtime_status")["write_every_seconds"]   # -> 20

Loaded once per process; reload() re-reads (tests). Every override is
checked against its default -- same type, a number above 0 where the default
is, a template using only the {fields} its default uses, a regex that
compiles. A bad value logs one line naming it and the default is used: a typo
in config.json must never stop the tray.
"""

import copy
import json
import logging
import os
import re
import string
import threading

import settings

DEFAULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime.defaults.json")
SETTINGS_KEY = "runtime"

log = logging.getLogger("denisse.runtime_config")

_lock = threading.Lock()
_cache = None


def defaults() -> dict:
    with open(DEFAULTS_FILE, encoding="utf-8") as f:
        return _strip(json.load(f))


def _strip(value):
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def _fields(template: str) -> set:
    return {name.split(".")[0].split("[")[0]
            for _, name, _, _ in string.Formatter().parse(template) if name}


def _bad(where, why, default):
    log.warning("config.json settings.%s.%s %s -- using the default %r", SETTINGS_KEY, where, why, default)
    return copy.deepcopy(default)


def _check(where, value, default):
    """value if it has the shape of default, else default (logged). `where`
    is the dotted path under settings.runtime, for the log line."""
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return _bad(where, "should be an object", default)
        out = {k: (_check(f"{where}.{k}", value[k], d) if k in value else copy.deepcopy(d))
               for k, d in default.items()}
        protos = {type(d) for d in default.values()}
        for k, v in value.items():
            if k in default or k.startswith("_"):
                continue
            if len(protos) == 1 and not issubclass(next(iter(protos)), (dict, list)):
                # an open map (e.g. a limit per job id): new keys take the same type
                proto = next(iter(default.values()))
                checked = _check(f"{where}.{k}", v, proto)
                if checked is v or checked == v:
                    out[k] = checked
            else:
                log.warning("config.json settings.%s.%s.%s isn't a known key -- ignored", SETTINGS_KEY, where, k)
        return out
    if isinstance(default, list):
        if not isinstance(value, list):
            return _bad(where, "should be a list", default)
        if not default:
            return copy.deepcopy(value)
        proto = default[0]
        out = []
        for i, v in enumerate(value):
            if isinstance(proto, dict):
                if not isinstance(v, dict) or set(v) - {k for k in v if k.startswith("_")} != set(proto):
                    return _bad(f"{where}[{i}]", f"needs exactly the keys {sorted(proto)}", default)
                checked = {k: _check(f"{where}[{i}].{k}", v[k], proto[k]) for k in proto}
                if checked != {k: v[k] for k in proto}:
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
        try:
            extra = _fields(value) - _fields(default)
        except ValueError as exc:
            return _bad(where, f"isn't a valid template ({exc})", default)
        if extra:
            return _bad(where, f"uses unknown fields {sorted(extra)}", default)
        return value
    return value


def _extra_checks(data, dflt):
    conv = data["conversation"]
    good = []
    for i, p in enumerate(conv["deflection_patterns"]):
        try:
            re.compile(p)
        except re.error as exc:
            log.warning("config.json settings.%s.conversation.deflection_patterns[%d] %r doesn't compile (%s) -- dropped",
                        SETTINGS_KEY, i, p, exc)
            continue
        good.append(p)
    conv["deflection_patterns"] = good
    si = data["standing_instructions"]
    if any(m > si["max_every_seconds"] for m in si["min_every_seconds"].values()):
        si["max_every_seconds"] = _bad("standing_instructions.max_every_seconds",
                                       "is below a min_every_seconds value", dflt["standing_instructions"]["max_every_seconds"])
        si["min_every_seconds"] = copy.deepcopy(dflt["standing_instructions"]["min_every_seconds"])
    return data


def load() -> dict:
    """The defaults with config.json's settings.runtime laid over them."""
    dflt = defaults()
    try:
        override = settings.get(SETTINGS_KEY)
    except (OSError, ValueError) as exc:
        log.warning("config.json unreadable (%s) -- runtime settings use their defaults", exc)
        override = None
    if override is None:
        return dflt
    if not isinstance(override, dict):
        log.warning("config.json settings.%s should be an object -- using the defaults", SETTINGS_KEY)
        return dflt
    for k in override:
        if k not in dflt and not k.startswith("_"):
            log.warning("config.json settings.%s.%s isn't a known section -- ignored", SETTINGS_KEY, k)
    data = {sec: (_check(sec, override[sec], d) if sec in override else d) for sec, d in dflt.items()}
    return _extra_checks(data, dflt)


def get(section: str) -> dict:
    """One section ("standing_instructions", "conversation", "runtime_status")."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = load()
        return _cache[section]


def reload() -> dict:
    global _cache
    with _lock:
        _cache = load()
        return _cache


def wording(section: str, key: str, **fields) -> str:
    """A configured template filled in; a template that won't format with
    these fields (e.g. a format spec for the wrong type) logs and uses the
    default."""
    template = get(section)["wording"][key]
    try:
        return template.format(**fields)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        log.warning("config.json settings.%s.%s.wording.%s won't format (%s) -- using the default",
                    SETTINGS_KEY, section, key, exc)
        return defaults()[section]["wording"][key].format(**fields)
