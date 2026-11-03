"""Load a JSON data file checked against its shipped defaults (AGENTS.md Rule 0).

Used by hotkey.py / explain_hotkey.py (hotkeys.json) and desktop_context.py
(desktop_context.json). Each data file ships with a ``*.defaults.json``
twin holding the same values; that twin is the documented fallback.

Every value is checked against the shape of its default:

  object   same keys (missing -> default, unknown -> warned and ignored)
  number   a number (whole if the default is whole), above 0 if the default is
  bool     true/false
  text     text; a ``{field}`` template may only use the default's fields
  list     a list whose items have the type of the default's first item

A bad value, a missing or unreadable file: one warning naming the key and
the default used instead. ``extra`` lets the caller add checks the shape
can't express (a regex that must compile, a hotkey that must parse).
Nothing here raises. Keys starting with ``_`` are notes and are dropped.
"""
from __future__ import annotations

import copy
import json
import logging
import string
from typing import Callable, Optional

log = logging.getLogger("denisse.json_config")


def _read(path) -> object:
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _fields(template: str) -> set:
    return {name.split(".")[0].split("[")[0]
            for _, name, _, _ in string.Formatter().parse(template) if name}


class Checker:
    """Shape checks for one file; ``bad`` logs and returns the default."""

    def __init__(self, label: str, warn: Optional[Callable[[str], None]] = None):
        self.label = label
        self._warn = warn or log.warning

    def bad(self, where: str, why: str, default):
        self._warn(f"{self.label}: {where} {why} -- using the default {default!r}")
        return copy.deepcopy(default)

    def check(self, where: str, value, default):
        if isinstance(default, dict):
            if not isinstance(value, dict):
                return self.bad(where, "should be an object", _strip(default))
            for k in value:
                if not k.startswith("_") and k not in default:
                    self._warn(f"{self.label}: {where}.{k} is not a known key -- ignored")
            out = {}
            for k, d in default.items():
                if k.startswith("_"):
                    continue
                if k not in value:
                    out[k] = self.bad(f"{where}.{k}", "is missing", _strip(d))
                else:
                    out[k] = self.check(f"{where}.{k}", value[k], d)
            return out
        if isinstance(default, list):
            if not isinstance(value, list):
                return self.bad(where, "should be a list", default)
            if default and not all(type(v) is type(default[0]) for v in value):
                return self.bad(where, f"should hold only {type(default[0]).__name__} items", default)
            return list(value)
        if isinstance(default, bool):
            return value if isinstance(value, bool) else self.bad(where, "should be true or false", default)
        if isinstance(default, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return self.bad(where, f"should be a number, got {value!r}", default)
            if isinstance(default, int) and not float(value).is_integer():
                return self.bad(where, f"should be a whole number, got {value!r}", default)
            if default > 0 and value <= 0:
                return self.bad(where, f"should be above 0, got {value!r}", default)
            return int(value) if isinstance(default, int) else float(value)
        if isinstance(default, str):
            if not isinstance(value, str):
                return self.bad(where, f"should be text, got {value!r}", default)
            try:
                extra = _fields(value) - _fields(default)
            except ValueError as exc:
                return self.bad(where, f"isn't a valid template ({exc})", default)
            if extra:
                return self.bad(where, f"uses unknown fields {sorted(extra)}", default)
            return value
        return value


def _strip(value):
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if not k.startswith("_")}
    return value


def load(label: str, path, defaults_path,
         extra: Optional[Callable[[dict, dict, Checker], dict]] = None,
         warn: Optional[Callable[[str], None]] = None) -> dict:
    """The checked contents of ``path``; never raises.

    If the defaults file itself is unreadable, ``path`` is used unchecked
    (and an empty dict if that fails too -- callers then use .get()).
    """
    checker = Checker(label, warn)
    try:
        defaults = _read(defaults_path)
        if not isinstance(defaults, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        log.error("%s: defaults %s unreadable (%s) -- using %s unchecked", label, defaults_path, exc, path)
        try:
            data = _read(path)
            return _strip(data) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}
    try:
        live = _read(path)
    except (OSError, ValueError) as exc:
        checker._warn(f"{label}: {path} unreadable ({exc}) -- using the shipped defaults")
        live = defaults
    data = checker.check(label, live, defaults)
    if extra is not None:
        try:
            data = extra(data, _strip(defaults), checker)
        except Exception as exc:  # a bug in a check must not take startup down
            checker._warn(f"{label}: extra checks failed ({exc}) -- using the shipped defaults")
            data = _strip(defaults)
    return data
