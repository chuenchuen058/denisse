"""voice.json, loaded once per process and validated.

The voice path (speech_control, voice_pipeline, wake_word, voice_output,
voice_mode) reads its words, thresholds, rates, timeouts and voices from
voice.json beside this file (AGENTS.md Rule 0). DENISSE_VOICE_CONFIG points
at another file (tests, a private tweak).

voice_defaults.json is the documented fallback, the same values voice.json
ships with (scripts/test_voice_config.py keeps the two equal). Every value
read from voice.json is checked against the type of its default: a wrong
type, a number out of range, an unknown key, a missing or unreadable file
each log one warning line naming the key and the default used instead.
Nothing here raises.
"""
from __future__ import annotations

import copy
import functools
import json
import logging
import math
import os
from pathlib import Path

log = logging.getLogger("denisse.voice_config")

DEFAULTS_PATH = Path(__file__).with_name("voice_defaults.json")


def _read_defaults() -> dict:
    try:
        with open(DEFAULTS_PATH, encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}
        log.error("voice config: %s is not a JSON object; voice.json is used unchecked", DEFAULTS_PATH)
    except (OSError, ValueError) as e:
        log.error("voice config: %s unreadable (%s); voice.json is used unchecked", DEFAULTS_PATH, e)
    return {}


DEFAULTS: dict = _read_defaults()

# Numbers must be > 0, except these, which may also be 0 (0 = none / off).
MAY_BE_ZERO = {"name_extra_letters", "lead_in_s"}


def config_path() -> Path:
    return Path(os.environ.get("DENISSE_VOICE_CONFIG") or Path(__file__).with_name("voice.json"))


def _warn(msg: str) -> None:
    log.warning("voice config: %s", msg)


def _short(value, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _check(key: str, value, default):
    """`value` if it has the shape of `default`, else None (and why)."""
    if isinstance(default, bool):
        return (value, None) if isinstance(value, bool) else (None, "must be true or false")
    if isinstance(default, (int, float)):
        if not _is_number(value):
            return None, "must be a number"
        if isinstance(default, int) and not float(value).is_integer():
            return None, "must be a whole number"
        if value < 0 or (value == 0 and key not in MAY_BE_ZERO):
            return None, "must be above 0" if key not in MAY_BE_ZERO else "must not be negative"
        return (int(value) if isinstance(default, int) else float(value)), None
    if isinstance(default, str):
        return (value, None) if isinstance(value, str) else (None, "must be text")
    if isinstance(default, list):
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            return value, None
        return None, "must be a list of text"
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return None, "must be an object"
        sample = next(iter(default.values()), "")
        out = {}
        for k, v in value.items():
            if k.startswith("_") and k != "_default":
                continue
            good, why = _check(key, v, default.get(k, sample))
            if why:
                return None, f"entry {k!r} {why}"
            out[k] = good
        return out, None
    return value, None


def _merge(data) -> dict:
    out = copy.deepcopy(DEFAULTS)
    if not isinstance(data, dict):
        _warn(f"{config_path()} is not a JSON object; using every default")
        return out
    if not DEFAULTS:  # no reference to check against (logged at import): take the file as written
        return {s: {k: v for k, v in e.items() if not k.startswith("_")}
                for s, e in data.items() if not s.startswith("_") and isinstance(e, dict)}
    for section, entries in data.items():
        if section.startswith("_"):
            continue
        if section not in DEFAULTS:
            _warn(f"unknown section {section!r} ignored")
            continue
        if not isinstance(entries, dict):
            _warn(f"section {section!r} is not an object; using its defaults")
            continue
        for key, value in entries.items():
            if key.startswith("_"):
                continue
            if key not in DEFAULTS[section]:
                _warn(f"unknown key {section}.{key} ignored")
                continue
            default = DEFAULTS[section][key]
            good, why = _check(key, value, default)
            if why:
                _warn(f"{section}.{key} = {_short(value)} {why}; using the default {_short(default)}")
                continue
            out[section][key] = good
    return out


@functools.lru_cache(maxsize=None)
def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        _warn(f"{path} not found; using every default")
        return copy.deepcopy(DEFAULTS)
    except (OSError, ValueError) as e:
        _warn(f"{path} unreadable ({e}); using every default")
        return copy.deepcopy(DEFAULTS)
    return _merge(data)


def section(name: str) -> dict:
    """The validated values of one section, e.g. section("wake_word")["follow_up_timeout_s"]."""
    return _load(str(config_path()))[name]


def reload() -> None:
    """Forget the cached file (tests; after editing voice.json in a live process
    a module that copied values at import also needs importlib.reload)."""
    _load.cache_clear()
