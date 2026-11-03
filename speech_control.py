"""In-process speech cancellation shared by chat, wake word and playback.

A reply captures an epoch before composing/synthesizing. A stop invalidates
that epoch, including audio which has not finished synthesizing yet.

What counts as a spoken/typed stop is data: voice.json "stop_request"
(AGENTS.md Rule 0). It stays a whole-utterance rule rather than a question
for the model, because a stop has to act before and without the model --
on 2026-10-31 a bare "stop" went to the model, which carried on.
"""
import functools
import re
import threading

import voice_config

_lock = threading.Lock()
_epoch = 0


def capture():
    with _lock:
        return _epoch


def stop():
    global _epoch
    with _lock:
        _epoch += 1


def cancelled(epoch):
    return epoch != capture()


def _words(phrase):
    return " ".join(re.sub(r"[^a-z\s]", " ", str(phrase).lower()).split())


def _alt(phrases):
    """One regex alternative over the phrases, longest first; None if empty."""
    cleaned = sorted({_words(p) for p in phrases if _words(p)}, key=len, reverse=True)
    return "(?:" + "|".join(re.escape(p) for p in cleaned) + ")" if cleaned else None


@functools.lru_cache(maxsize=4)
def _rule(path):
    """(typo fixes, compiled whole-utterance pattern or None) for one config file."""
    cfg = voice_config.section("stop_request")
    phrases = _alt(cfg["phrases"])
    if phrases is None:
        return (), None
    lead, reason, trail = _alt(cfg["lead_words"]), _alt(cfg["reasons"]), _alt(cfg["trail_words"])
    pattern = ((f"(?:{lead} )*" if lead else "") + (f"(?:{reason} )?" if reason else "")
               + phrases + (f"(?: {trail})*" if trail else ""))
    typos = tuple((_words(k), _words(v)) for k, v in cfg["typos"].items() if _words(k))
    return typos, re.compile(pattern)


def is_stop_request(text):
    """True when the whole of `text` asks her to stop speaking -- not
    'stop the timer', "don't stop talking" or a quoted stop inside a question."""
    typos, pattern = _rule(str(voice_config.config_path()))
    if pattern is None:
        return False
    text = _words(text or "")
    for wrong, right in typos:
        text = text.replace(wrong, right)
    return bool(pattern.fullmatch(text))
