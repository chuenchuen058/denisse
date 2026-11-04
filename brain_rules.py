"""Validated local preferences; text in this file is never a model prompt.

Two files, two jobs:

* memory/brain_rules.json (private) -- his settings: how many turns she
  sees, whether facts are saved, vault search on or off. Edit it using
  brain_rules.example.json as the template. Invalid edits retain the last
  valid settings and are visible through status(), rather than crashing the
  tray or granting new powers. ``load()`` / ``status()``.
* brain.json (in the repo) -- what brain.py says, matches and limits
  outside the system prompt and tool descriptions (AGENTS.md Rule 0):
  reply nets, the requests answered before the model, her fixed wording,
  thresholds. Checked by json_config.py against brain.defaults.json; a bad
  or missing value falls back with one warning. ``data()`` reads it;
  brain.configure() rebinds brain's module values from it.
"""
import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass
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


@dataclass(frozen=True)
class Rules:
    recent_turns: int = 6
    style_examples: bool = True
    save_facts: str = "explicit"
    vault_search: bool = True
    vault_max_notes: int = 3
    vault_max_chars: int = 1200
    context_max_age_days: int = 14
    self_learning: bool = True
    learning_confirmations: int = 2


_lock = threading.RLock()
_valid = {}
_errors = {}


def rules_path():
    return Path(os.environ.get("DENISSE_RULES_FILE") or
                Path(__file__).parent / "memory" / "brain_rules.json")


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
    for key, lo, hi in (("recent_turns", 1, 10), ("vault_max_notes", 1, 5),
                        ("vault_max_chars", 400, 2000), ("context_max_age_days", 1, 90),
                        ("learning_confirmations", 2, 10)):
        if not lo <= values[key] <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
    if values["save_facts"] not in ("explicit", "conversation"):
        raise ValueError("save_facts must be explicit or conversation")
    return Rules(**values)


def load():
    path = rules_path()
    key = str(path.resolve())
    with _lock:
        try:
            with path.open(encoding="utf-8-sig") as f:
                content = f.read(8193)
            if len(content) > 8192:
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
    """An instruction to retain a fact, not a question asking for recall."""
    import re
    return bool(re.match(
        r"^\s*(?:(?:please|hey denisse)[,\s]+|(?:can|could|would|will) you\s+)?"
        r"(?:remember\b(?!\s+(?:what|when|where|who|how)\b)|"
        r"(?:save|store)\b.{0,40}\b(?:fact|memory|preference)\b|keep in mind\b)",
        text or "", re.I))
