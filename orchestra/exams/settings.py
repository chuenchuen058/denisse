"""Policy data for this subsystem; read on use so edits need no Python changes.

Missing or invalid data raises instead of silently restoring hardcoded defaults.
"""
import json
from pathlib import Path

DATA_FILE = Path(__file__).with_name("rules.json")


def load() -> dict:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))
