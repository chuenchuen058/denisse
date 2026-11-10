"""The checks every job's scorer is built from. A case is a dict of named checks (True/False);
its score is the share that passed, and it is ok when all did. Thresholds in bench.json "score".
"""
from __future__ import annotations

import re
import statistics

_WORD = re.compile(r"\w+", re.UNICODE)


def words(text, min_chars: int) -> set:
    return {w for w in _WORD.findall(str(text or "").casefold()) if len(w) >= min_chars}


def flatten(value) -> str:
    """Every string in a nested reply, joined: what the scorer reads."""
    if isinstance(value, dict):
        return "\n".join(flatten(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(flatten(v) for v in value)
    return "" if value is None or isinstance(value, bool) else str(value)


def has_terms(text: str, terms: list) -> bool:
    """Each expected term (a word or phrase from the fixture) appears, ignoring case and spacing."""
    hay = " ".join(str(text or "").casefold().split())
    return all(" ".join(str(t).casefold().split()) in hay for t in terms)


def grounded_share(answer: str, source: str, min_chars: int) -> float:
    """Share of the answer's words that the source also has (1.0 for an empty answer)."""
    said = words(answer, min_chars)
    if not said:
        return 1.0
    return len(said & words(source, min_chars)) / len(said)


def grounded(answer: str, source: str, policy: dict) -> bool:
    return grounded_share(answer, source, policy["min_word_chars"]) >= policy["ground_min_share"]


def case(case_id: str, checks: dict, seconds: float, error: str | None = None, **detail) -> dict:
    n = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"id": case_id, "checks": checks, "score": round(passed / n, 3) if n else 0.0,
            "ok": bool(n) and passed == n and error is None, "seconds": round(seconds, 2),
            "error": error, **({"detail": detail} if detail else {})}


def summarise(cases: list) -> dict:
    """One job's line: mean score, ok count, median seconds per case."""
    if not cases:
        return {"score": None, "ok": 0, "cases": 0, "errors": 0, "median_s": None}
    secs = [c["seconds"] for c in cases]
    return {"score": round(statistics.mean(c["score"] for c in cases), 3),
            "ok": sum(1 for c in cases if c["ok"]), "cases": len(cases),
            "errors": sum(1 for c in cases if c.get("error")),
            "median_s": round(statistics.median(secs), 2)}
