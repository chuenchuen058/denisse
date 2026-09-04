"""
One brain, one conversation -- shared by every face of Denisse.

Before this module there were four independent `Brain()` instances (the tray
popup, the wake-word listener, the daily news briefing, and the CLI), each
doing its own `memory_store.load_history()` at start and its own
`save_history()` at the end. Two consequences, both observed on 2026-09-04:

1. Last writer wins. Two faces active at once (popup open while the wake
   word is listening) meant one silently overwrote the other's turns.

2. Worse, and the reason the tray popup felt like it had no personality:
   the *whole* stored history was replayed to the model every turn, raw
   tool traffic included. `conversation.json` had filled up with garbled
   Vosk transcripts ("the is not playing anything the") answered by a
   reflexive get_current_time call and a flat "It seems like you're trying
   to say something..." -- and llama3.1:8b imitates the nearest examples in
   its context over instructions in a system prompt it saw 4000 tokens ago.

   Measured directly, same brain, same model, only the history differing:

       "hey"  with empty history -> "Doing alright, you?"          (no tools)
       "hey"  with real history  -> recall() then "It seems like I
                                     don't have any information
                                     stored for 'hey'."
       "what do you think of my setup?" with real history -> read_file()
                                     on "/home/user/setup.txt" -- a Linux
                                     path, on a Windows machine.

So: `replay()` decides what the model *sees* (recent, and prose only),
`record()` decides what goes on *disk* (everything, appended, never
clobbered). Keeping those two separate is the whole point of the module.
"""

import threading

import memory_store

# How many past turns to replay. A turn is a user message plus everything
# the brain produced before the next user message. Small on purpose: the
# model is served with a 4096-token context, and the failure above was a
# context-crowding failure, not a memory-capacity one.
REPLAY_TURNS = 6

_lock = threading.RLock()
_brain = None


def get_brain():
    """The one Brain for this process, built on first use.

    Construction hits Ollama (Brain.__init__ checks the server is up and the
    model is pulled), so it stays lazy -- importing this module must not
    require a running Ollama.
    """
    global _brain
    with _lock:
        if _brain is None:
            from brain import Brain

            _brain = Brain()
        return _brain


def _is_turn_start(msg: dict) -> bool:
    return msg.get("role") == "user"


def _prose_only(messages: list) -> list:
    """Strip tool traffic from a replay slice.

    Drops `tool` results and the assistant messages that requested them,
    leaving plain user/assistant prose. Both halves go together, so what's
    left is still a valid thing to replay -- and, more to the point, the
    model no longer sees a transcript of itself calling get_current_time at
    every prompt, which is the habit it was copying.

    Nothing is deleted from disk: this shapes the replay only.
    """
    out = []
    for m in messages:
        if m.get("role") == "tool":
            continue
        if m.get("role") == "assistant" and m.get("tool_calls"):
            continue
        if m.get("role") == "assistant" and not (m.get("content") or "").strip():
            continue
        out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


def replay(turns: int = REPLAY_TURNS) -> list:
    """The context to hand the brain for a new turn: the last `turns`
    exchanges, prose only. Always a fresh list -- callers append their new
    user message to it and hand it straight to Brain.respond()."""
    with _lock:
        history = memory_store.load_history()

    starts = [i for i, m in enumerate(history) if _is_turn_start(m)]
    if len(starts) > turns:
        history = history[starts[-turns]:]
    return _prose_only(history)


# Filler words a Vosk mis-transcription collapses into. Not a blocklist for
# what Denisse will answer -- she still replies to anything -- just for what
# is worth writing down and replaying later.
_FILLER = {
    "the", "a", "an", "uh", "um", "ah", "oh", "eh", "hmm", "mm",
    "is", "it", "to", "and", "or", "of", "in", "on", "you", "i",
}


def looks_like_noise(text: str) -> bool:
    """True only for transcripts that are pure filler -- "", "the", "the uh
    the". Those get answered but not written down.

    Deliberately weak, and it stays weak on purpose. A ratio test ("mostly
    filler") does catch more of the real mishearings, e.g. "the is not
    playing anything the" -- but the same test starts eating short real
    commands, and dropping something Chuen actually said is a worse failure
    than keeping something he didn't. This gate is defence in depth, not the
    fix: replay() is what stopped the poisoning, and it does so even against
    the fully poisoned history (measured). So this only removes what is
    unambiguously nothing.
    """
    words = [w for w in "".join(c if c.isalnum() else " " for c in text.lower()).split() if w]
    if not words:
        return True
    return not [w for w in words if w not in _FILLER]


def record(new_messages: list, source: str = "typed") -> bool:
    """Append one completed turn to the stored history and return whether it
    was written.

    `new_messages` is the tail Brain.respond produced for this turn --  the
    user message and everything after it. Re-reads the file under the lock
    and appends, rather than writing back a list loaded minutes ago, so a
    popup reply can't erase what the wake-word listener just recorded.

    `source="voice"` runs the transcript through looks_like_noise() first;
    a mishearing is answered out loud but left out of the record.
    """
    if not new_messages:
        return False

    if source == "voice":
        first_user = next((m for m in new_messages if m.get("role") == "user"), None)
        if first_user is not None and looks_like_noise(first_user.get("content") or ""):
            return False

    with _lock:
        history = memory_store.load_history()
        history.extend(new_messages)
        memory_store.save_history(history)
    return True


def reset(archive: bool = True) -> str | None:
    """Start a fresh conversation. Returns the archive path, or None.

    Kept as an explicit call rather than something that happens on its own:
    the history is Denisse's memory, and throwing it away is Chuen's
    decision, not a cleanup step.
    """
    import datetime
    import os
    import shutil

    with _lock:
        if not os.path.exists(memory_store.MEMORY_FILE):
            return None
        dest = None
        if archive:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
            dest = os.path.join(memory_store.MEMORY_DIR, f"conversation.archive-{stamp}.json")
            shutil.copy2(memory_store.MEMORY_FILE, dest)
        memory_store.clear_history()
        return dest
