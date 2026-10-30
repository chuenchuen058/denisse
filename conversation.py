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

import logging
import re
import threading
import uuid

import brain_rules
import memory_store

# How many past turns to replay. A turn is a user message plus everything
# the brain produced before the next user message. Small on purpose: the
# model is served with an 8192-token context, and the failure above was a
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
    return msg.get("role") == "user" and (msg.get("content") or "").strip().lower() not in ("/good", "/bad")


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
    feedback_turn = False
    for m in messages:
        if m.get("role") == "user":
            feedback_turn = (m.get("content") or "").strip().lower() in ("/good", "/bad")
        if feedback_turn:
            continue  # learner sees these on disk; the model needn't imitate them
        if m.get("role") == "tool":
            continue
        if m.get("role") == "assistant" and m.get("tool_calls"):
            continue
        if m.get("role") == "assistant" and not (m.get("content") or "").strip():
            continue
        out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


# Replies that did nothing for him. Kept on disk (the learner and the eval
# read them there), left out of what the model sees.
_DEFLECTION = re.compile(
    r"didn'?t\s+(?:quite\s+)?catch|\brephrase\b|say\s+that\s+again|ask\s+that\s+again"
    r"|i\s+don'?t\s+have\s+(?:any\s+|enough\s+)?(?:information|info|details|access)"
    r"|can\s+you\s+tell\s+me\s+(?:more|what)|i'?m\s+not\s+(?:actually\s+)?able\s+to"
    r"|it\s+seems\s+(?:like\s+)?i\s+don'?t\s+have|i\s+got\s+stuck"
    r"|i\s+haven'?t\s+confirmed\s+that\s+action|i\s+couldn'?t\s+complete"
    r"|\b[a-z]+_[a-z_]+'?\s+tool\b",  # "use the 'list_skills' tool"
    re.IGNORECASE)


def _turns(history: list) -> list:
    """Split a history into turns: each starts at a user message (feedback
    included) and runs to the next. Anything before the first user message
    is its own leading group."""
    groups, cur = [], []
    for m in history:
        if m.get("role") == "user" and cur:
            groups.append(cur)
            cur = []
        cur.append(m)
    if cur:
        groups.append(cur)
    return groups


def _reply_of(turn: list) -> str:
    texts = [(m.get("content") or "") for m in turn
             if m.get("role") == "assistant" and not m.get("tool_calls")]
    return texts[-1] if texts else ""


def _drop_failed(groups: list) -> list:
    """Leave out the turns that went wrong, so the model doesn't build on them.

    Traced 2026-10-30: "do you like rainy days?" got "I didn't catch that.
    Could you rephrase?" 4/4 against the live replay and 0/4 with one bad
    turn removed ("hows my internship tracker" -> "I don't have any
    information about your internship tracker"). Three kinds go:

    - a turn whose reply was a deflection (_DEFLECTION);
    - a turn he complained about ("thats not what im asking"), and the
      complaint turn itself -- its reply answers a request that's gone;
    - a turn he marked /bad;
    - her skills list given to something that wasn't a skills question
      ("plan my internship applications", 2026-10-29) -- execute() refuses
      that now, but the old turns are still on disk.
    """
    try:
        from brain import _COMPLAINT, _SKILL_QUESTION
    except Exception:  # never let a broken brain import stop the replay
        _COMPLAINT = _SKILL_QUESTION = None
    keep = [True] * len(groups)
    for i, g in enumerate(groups):
        first = g[0]
        text = (first.get("content") or "").strip() if first.get("role") == "user" else ""
        if text.lower() == "/bad" and i > 0:
            keep[i - 1] = False
        if text and _COMPLAINT is not None and _COMPLAINT.fullmatch(text):
            keep[i] = False
            if i > 0:
                keep[i - 1] = False
        reply = _reply_of(g)
        if _DEFLECTION.search(reply):
            keep[i] = False
        if (_SKILL_QUESTION is not None and reply.startswith("Here's what I can help with")
                and not _SKILL_QUESTION.search(text)):
            keep[i] = False
    return [g for g, k in zip(groups, keep) if k]


def replay(turns: int | None = None) -> list:
    """The context to hand the brain for a new turn: the last `turns`
    exchanges that went right, prose only. Always a fresh list -- callers
    append their new user message to it and hand it straight to
    Brain.respond()."""
    if turns is None:
        turns = brain_rules.load().recent_turns
    with _lock:
        history = memory_store.load_history()

    # The window stays the last `turns` turns; failed ones inside it are
    # dropped, not replaced. Refilling from further back pulled in older
    # junk (an invented weather briefing, "use the list_skills tool").
    groups = _turns(history)
    good = {id(g) for g in _drop_failed(groups)}
    recent = [g for g in groups if _is_turn_start(g[0])][-turns:]
    return _prose_only([m for g in recent if id(g) in good for m in g])


# Filler words a Vosk mis-transcription collapses into. Not a blocklist for
# what Denisse will answer -- she still replies to anything -- just for what
# is worth writing down and replaying later.
_FILLER = {
    "the", "a", "an", "uh", "um", "ah", "oh", "eh", "hmm", "mm",
    "is", "it", "to", "and", "or", "of", "in", "on", "you", "i",
}


def last_attempt(user_text: str) -> list:
    """The tool calls the latest stored turn for `user_text` made, each as
    {"name", "arguments", "result"}. replay() is prose only, so when he
    says "thats not what im asking" the failed calls exist only on disk --
    and a retry that can't see them can repeat them exactly."""
    want = " ".join((user_text or "").lower().split())
    with _lock:
        history = memory_store.load_history()
    start = None
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if m.get("role") == "user" and " ".join((m.get("content") or "").lower().split()) == want:
            start = i
            break
    if start is None:
        return []
    calls, results = [], []
    for m in history[start + 1:]:
        if m.get("role") == "user":
            break
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                f = c.get("function") or {}
                if f.get("name"):
                    calls.append({"name": f["name"], "arguments": f.get("arguments") or {}})
        elif m.get("role") == "tool":
            results.append(m.get("content") or "")
    for call, res in zip(calls, results):
        call["result"] = res
    return calls


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
        # IDs are storage metadata only; _prose_only never sends them to
        # the model. They distinguish independent lessons from rereads.
        stored = [dict(m) for m in new_messages]
        for m in stored:
            if m.get("role") == "user":
                m.setdefault("turn_id", uuid.uuid4().hex)
        history.extend(stored)
        memory_store.save_history(history)
    try:
        import instincts
        instincts.rebuild(history)
    except Exception as exc:
        logging.warning("Learning observation deferred: %s", exc)
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
