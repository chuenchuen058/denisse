"""
Simple persistent conversation memory.

Stores the running message history as JSON on disk so Denisse remembers
context across restarts, without needing a database for v1.
"""

import json
import os

MEMORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")
MEMORY_FILE = os.path.join(MEMORY_DIR, "conversation.json")

MAX_STORED_MESSAGES = 200  # keep the file bounded


def load_history() -> list:
    if not os.path.exists(MEMORY_FILE):
        return []
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_history(messages: list) -> None:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    trimmed = messages[-MAX_STORED_MESSAGES:]
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, indent=2)


def clear_history() -> None:
    if os.path.exists(MEMORY_FILE):
        os.remove(MEMORY_FILE)
