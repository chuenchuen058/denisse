"""
How Chuen wants Denisse to talk: his standing instructions, kept as data
and read into every prompt.

"you dont have to say the file path", "keep it short and simple", "ask if
i want the path next time", "say me instead of Chuen" -- each of these was
said more than once on 2026-10-31, and the model agreed every time and
did it again the next reply. One became a task ("ask for file path next
time"), one was written into her own settings file, one into a text file
on his desk. None of those is where an instruction about how to reply
belongs. This store is.

Two ways in: the model's reply_preference tool (for anything worded in a
way nobody listed), and the fast path in brain.py (phrases in
tool_policy.json, so it works with Ollama down and answers at once). One
way out: prompt_block() is appended to the system prompt on every turn, so
the preferences are in front of the model whenever it writes a reply.
Nothing here is persisted into the conversation.

The file is private (memory/, gitignored). Capped so a wrong one can't
pile up unseen: the oldest goes when a new one arrives past the cap, and
"forget that" removes one by its words.
"""

import datetime
import json
import os
import re
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
# DENISSE_REPLY_PREFERENCES points the store elsewhere (the checks sandbox,
# the eval's scratch folder); the default is private, under memory/.
FILE = os.environ.get("DENISSE_REPLY_PREFERENCES") or os.path.join(ROOT, "memory", "reply_preferences.json")
MAX_ITEMS = 12
MAX_CHARS = 200  # one instruction, not an essay -- the prompt pays for every word

_lock = threading.Lock()
_WORD = re.compile(r"[a-z0-9']+")


def _words(text: str) -> set:
    return set(_WORD.findall((text or "").lower()))


def load() -> list:
    """[{"text": ..., "at": ...}, ...], oldest first. Unreadable -> []."""
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"].strip()]


def _save(items: list) -> None:
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FILE)


def _clean(text: str) -> str:
    t = " ".join((text or "").split()).strip(" .!")
    return t[:MAX_CHARS]


def add(text: str) -> str:
    """Save one standing instruction. Returns what happened, in plain words."""
    t = _clean(text)
    if not t:
        return "Nothing to save: the preference was empty."
    with _lock:
        items = load()
        for p in items:
            if _words(p["text"]) == _words(t):
                return f"Already noted: {p['text']}"
        items.append({"text": t, "at": datetime.datetime.now().isoformat(timespec="minutes")})
        items = items[-MAX_ITEMS:]
        try:
            _save(items)
        except OSError as exc:
            return f"Error: couldn't save that preference ({exc}). Tell him it wasn't saved."
    return f"Noted, and I'll follow it from now on: {t}"


def remove(text: str) -> str:
    """Drop the saved preference his words best match (by word overlap)."""
    want = _words(text) - {"that", "this", "the", "one", "forget", "preference", "rule", "about"}
    with _lock:
        items = load()
        if not items:
            return "There are no saved reply preferences to remove."
        scored = [(len(want & _words(p["text"])), i) for i, p in enumerate(items)]
        best, idx = max(scored)
        if best == 0:
            return ("No saved preference matches that. Saved: "
                    + "; ".join(p["text"] for p in items))
        gone = items.pop(idx)
        try:
            _save(items)
        except OSError as exc:
            return f"Error: couldn't update the preferences file ({exc})."
    return f"Forgot: {gone['text']}"


def listing() -> str:
    items = load()
    if not items:
        return "No reply preferences saved."
    return "Reply preferences, oldest first:\n" + "\n".join(f"- {p['text']}" for p in items)


def prompt_block() -> str:
    """The lines added to the system prompt, or "" when nothing is saved."""
    items = load()
    if not items:
        return ""
    return ("Standing instructions from Chuen about how to reply -- follow every one, "
            "every time, without mentioning them:\n"
            + "\n".join(f"- {p['text']}" for p in items))


def tool(action: str = "add", text: str = "") -> str:
    """The reply_preference tool: action 'add' (default), 'remove' or 'list'."""
    a = (action or "add").strip().lower()
    if a in ("remove", "forget", "delete", "drop"):
        return remove(text)
    if a in ("list", "show"):
        return listing()
    return add(text)
