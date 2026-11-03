"""dialogue_rules.json, loaded once, plus the typo-tolerant word match
multistep.py and clarify.py share.

The file holds grammar and filler words and limits, never his phrasings
(AGENTS.md Rule 0). Each entry is {"why": ..., "value": ...}; get() returns
the values of one section. DENISSE_DIALOGUE_RULES points at another file
(tests, or a private tweak) -- read once per process like the default.
"""
import difflib
import functools
import json
import os
import re
from pathlib import Path


def rules_path() -> Path:
    return Path(os.environ.get("DENISSE_DIALOGUE_RULES") or Path(__file__).with_name("dialogue_rules.json"))


@functools.lru_cache(maxsize=None)
def _load(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    out = {}
    for section, entries in data.items():
        if section.startswith("_"):
            continue
        out[section] = {k: (v["value"] if isinstance(v, dict) and "value" in v else v)
                        for k, v in entries.items() if not k.startswith("_")}
    return out


def get(section: str) -> dict:
    """The values of one section, e.g. get("clarify")["expiry_seconds"]."""
    return _load(str(rules_path()))[section]


def squeeze(word: str) -> str:
    """A stretched word at its plain length: 'yesss' -> 'yes', 'noooo' -> 'no'."""
    return re.sub(r"(.)\1+", r"\1", word)


def closest(word: str, vocabulary, ratio: float, min_len: int):
    """The vocabulary word `word` is, allowing for stretched letters and,
    for words of at least `min_len` letters, a typo ('remnd' -> 'remind').
    None if it is none of them."""
    word = (word or "").lower()
    vocab = list(vocabulary)
    if word in vocab:
        return word
    flat = squeeze(word)
    for v in vocab:
        if squeeze(v) == flat:
            return v
    if len(word) < min_len:
        return None
    near = difflib.get_close_matches(word, [v for v in vocab if len(v) >= min_len - 1], n=1, cutoff=ratio)
    return near[0] if near else None
