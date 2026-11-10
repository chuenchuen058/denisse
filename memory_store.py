"""
Simple persistent conversation memory.

Stores the running message history as JSON on disk so Denisse remembers
context across restarts, without needing a database for v1.
"""

import os

import obsidian_sync
import safe_json

MEMORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")
MEMORY_FILE = os.path.join(MEMORY_DIR, "conversation.json")

MAX_STORED_MESSAGES = 200  # keep the file bounded


def load_history() -> list:
    """The stored history; [] if there is none. A file that won't parse is
    copied aside by safe_json (never silently replaced by the next save)."""
    return safe_json.read_json(MEMORY_FILE, [])


def locked():
    """The cross-process lock for a read-modify-write of the history: the
    tray, scripts/ask.py, the eval and the agent loop are separate
    processes. conversation.record() holds it."""
    return safe_json.locked(MEMORY_FILE)


def save_history(messages: list) -> None:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    trimmed = messages[-MAX_STORED_MESSAGES:]
    # A raw slice can start mid tool-call sequence: a "tool" message whose
    # answering assistant message got cut off. Drop any such orphans so the
    # history stays valid to replay.
    while trimmed and trimmed[0].get("role") == "tool":
        trimmed = trimmed[1:]
    safe_json.write_json_atomic(MEMORY_FILE, trimmed, indent=2)
    obsidian_sync.sync_conversation(trimmed)


def clear_history() -> None:
    if os.path.exists(MEMORY_FILE):
        os.remove(MEMORY_FILE)
