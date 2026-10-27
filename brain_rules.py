"""Validated local preferences; text in this file is never a model prompt.

Edit memory/brain_rules.json (private), using brain_rules.example.json as
the template. Invalid edits retain the last valid settings and are visible
through status(), rather than crashing the tray or granting new powers.
"""
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

# These permissions cannot be granted by a learned routing preference.
MUTATING_TOOLS = frozenset({
    "remember", "add_task", "complete_task", "remove_task", "write_file",
    "run_shell_command", "open_application", "set_timer", "cancel_timer",
    "play_song", "pause_music", "resume_music", "skip_song", "previous_song",
    "clean_temp_files", "disable_startup_program", "restore_startup_program",
})


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
