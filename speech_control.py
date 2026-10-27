"""In-process speech cancellation shared by chat, wake word and playback.

A reply captures an epoch before composing/synthesizing. A stop invalidates
that epoch, including audio which has not finished synthesizing yet.
"""
import re
import threading

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


def is_stop_request(text):
    text = re.sub(r"[^a-z\s]", " ", (text or "").lower())
    text = " ".join(text.split())
    text = text.replace("talkinhg", "talking").replace("talkjing", "talking")
    # Whole requests, not 'stop the timer', 'don't stop talking' or quoted
    # instructions inside a question. Include the actual observed typos.
    return bool(re.fullmatch(
        r"(?:(?:hey|okay|ok|please|denisse|no|can you|could you) )*"
        r"(?:(?:it is|its|it s) too long )?"
        r"(?:shush|hush|be quiet|quiet|stop (?:talking|speaking|speech)|"
        r"(?:you are|you re|youre) still (?:talking|speaking))"
        r"(?: (?:now|please|denisse))*", text))
