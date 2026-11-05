"""Validated local preferences; text in this file is never a model prompt.

Two files, two jobs:

* memory/brain_rules.json (private) -- his settings: how many turns she
  sees, whether facts are saved, vault search on or off. Edit it using
  brain_rules.example.json as the template. Invalid edits retain the last
  valid settings and are visible through status(), rather than crashing the
  tray or granting new powers. ``load()`` / ``status()``.
* brain.json (in the repo) -- what brain.py says, matches and limits
  (AGENTS.md Rule 0): the system prompt and tool descriptions, reply nets,
  the requests answered before the model, her fixed wording, thresholds;
  and this module's own data -- the settings file's path, limits, defaults
  and ranges (``settings``) and the fact-saving request pattern
  (``memory``). Checked by json_config.py against brain.defaults.json; a
  bad or missing value falls back with one warning. ``data()`` reads it;
  brain.configure() rebinds brain's module values from it;
  ``_cached_data()`` here re-reads it when the file changes.
"""
import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import json_config
import tool_policy

# These permissions cannot be granted by a learned routing preference.
# Every tool whose side_effect in tool_policy.json is "write".
MUTATING_TOOLS = tool_policy.mutating()

DATA_ENV_VAR = "DENISSE_BRAIN_DATA"
DATA_FILE = Path(__file__).with_name("brain.json")
DATA_DEFAULTS_FILE = Path(__file__).with_name("brain.defaults.json")


def _is_pattern_key(key: str) -> bool:
    return key in ("pattern", "patterns") or key.endswith(("_pattern", "_patterns"))


def _compile_all(value) -> None:
    """Every regex in `value` compiles: a pattern string (empty = never
    matches), a list of them, or a list of objects whose pattern keys hold
    them."""
    if isinstance(value, str):
        if value:
            re.compile(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                for k, inner in item.items():
                    if _is_pattern_key(k):
                        _compile_all(inner)
            else:
                _compile_all(item)


def _check_data(data: dict, defaults: dict, checker, where: str = "brain") -> dict:
    """The checks json_config's shapes can't express: every pattern
    compiles, and every object in a list of objects has the keys (with the
    types) of the default's first object. A bad value is replaced by its
    default, with one warning naming the key."""
    for key, value in list(data.items()):
        if key.startswith("_"):
            continue
        here = f"{where}.{key}"
        default = defaults.get(key) if isinstance(defaults, dict) else None
        if isinstance(value, dict) and isinstance(default, dict):
            _check_data(value, default, checker, here)
            continue
        if isinstance(value, list) and isinstance(default, list) and default and isinstance(default[0], dict):
            shape = {k: type(v) for k, v in default[0].items() if not k.startswith("_")}
            bad = next((f"item {i} is missing {k!r}" if k not in item else f"item {i}: {k!r} should be {t.__name__}"
                        for i, item in enumerate(value) for k, t in shape.items()
                        if not isinstance(item, dict) or k not in item or type(item[k]) is not t), None)
            if bad:
                data[key] = checker.bad(here, bad, default)
                continue
        if _is_pattern_key(key) or (isinstance(value, list) and value and isinstance(value[0], dict)):
            try:
                _compile_all(value)
            except re.error as e:
                data[key] = checker.bad(here, f"has a bad pattern ({e})", default)
    return data


def data(path=None) -> dict:
    """The checked contents of brain.json (or `path`, or $DENISSE_BRAIN_DATA)."""
    path = path or os.environ.get(DATA_ENV_VAR) or DATA_FILE
    return json_config.load("brain", path, DATA_DEFAULTS_FILE, extra=_check_data)


_data_cache = {"key": None, "data": None}


def _cached_data() -> dict:
    """data() for this module's own values, re-read when the file (or
    $DENISSE_BRAIN_DATA) changes rather than on every settings read."""
    path = os.environ.get(DATA_ENV_VAR) or DATA_FILE
    try:
        key = (str(path), os.stat(path).st_mtime_ns)
    except OSError:
        key = (str(path), None)
    with _lock:
        if _data_cache["key"] != key or key[1] is None:
            _data_cache.update(key=key, data=data(path))
        return _data_cache["data"]


def _default(key: str):
    return field(default_factory=lambda: _cached_data()["settings"]["defaults"][key])


@dataclass(frozen=True)
class Rules:
    """His settings; the defaults are brain.json settings.defaults."""
    recent_turns: int = _default("recent_turns")
    style_examples: bool = _default("style_examples")
    save_facts: str = _default("save_facts")
    vault_search: bool = _default("vault_search")
    vault_max_notes: int = _default("vault_max_notes")
    vault_max_chars: int = _default("vault_max_chars")
    context_max_age_days: int = _default("context_max_age_days")
    self_learning: bool = _default("self_learning")
    learning_confirmations: int = _default("learning_confirmations")


_lock = threading.RLock()
_valid = {}
_errors = {}


def rules_path():
    return Path(os.environ.get("DENISSE_RULES_FILE") or
                Path(__file__).parent / _cached_data()["settings"]["file"])


def validate(data):
    if not isinstance(data, dict):
        raise ValueError("rules must be a JSON object")
    defaults = asdict(Rules())
    unknown = set(data) - set(defaults)
    if unknown:
        raise ValueError("unsupported settings: " + ", ".join(sorted(unknown)))
    for key, value in data.items():
        if type(value) is not type(defaults[key]):
            raise ValueError(f"{key} must be {type(defaults[key]).__name__}")
    values = {**defaults, **data}
    settings = _cached_data()["settings"]
    for key, (lo, hi) in settings["ranges"].items():
        if not lo <= values[key] <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
    allowed = settings["save_facts_values"]
    if values["save_facts"] not in allowed:
        raise ValueError("save_facts must be " + " or ".join(allowed))
    return Rules(**values)


def load():
    path = rules_path()
    key = str(path.resolve())
    with _lock:
        try:
            limit = _cached_data()["settings"]["max_file_bytes"]
            with path.open(encoding="utf-8-sig") as f:
                content = f.read(limit + 1)
            if len(content) > limit:
                raise ValueError("rules file is too large")
            rules = validate(json.loads(content))
            _valid[key] = rules
            _errors.pop(key, None)
            return rules
        except FileNotFoundError:
            _errors.pop(key, None)
            _valid[key] = Rules()
            return _valid[key]
        except (OSError, ValueError) as exc:
            error = str(exc)
            if _errors.get(key) != error:
                logging.warning("Denisse rules edit ignored: %s", error)
            _errors[key] = error
            return _valid.get(key, Rules())


def status():
    rules = load()
    path = rules_path()
    return {"file": str(path), "error": _errors.get(str(path.resolve())),
            "settings": asdict(rules)}


def explicit_memory_request(text):
    """An instruction to retain a fact, not a question asking for recall:
    brain.json memory.explicit_request_pattern (empty never matches)."""
    pattern = _cached_data()["memory"]["explicit_request_pattern"]
    return bool(pattern) and bool(re.match(pattern, text or "", re.I))
