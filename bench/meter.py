"""Tokens and model time for the calls a job makes in this process, without changing them.

While `metered()` is open, ollama.chat (the Python client: Don, Denisse) and urllib's urlopen
for an Ollama chat URL (the local team, Atrium) are wrapped: same arguments, same reply; the
reply's eval_count / eval_duration / prompt_eval_count go into eval/speed.py's Profile, which
already does that sum for eval/run_eval.py. VRAM comes from Ollama's /api/ps when it reports it.
"""
from __future__ import annotations

import contextlib
import io
import json
import time
import urllib.request

from eval.speed import Profile


def figures(prof: Profile) -> dict:
    gen_s = prof.gen_s
    return {"calls": prof.calls, "gen_tokens": prof.gen_tokens, "prompt_tokens": prof.prompt_tokens,
            "model_s": round(prof.model_s, 2), "gen_s": round(gen_s, 2),
            "tokens_per_s": round(prof.gen_tokens / gen_s, 1) if gen_s > 0 else None}


class _Reply(io.BytesIO):
    """The body a wrapped urlopen already read, handed back as the response."""

    def __init__(self, body: bytes, real):
        super().__init__(body)
        self.status = getattr(real, "status", None)
        self.headers = getattr(real, "headers", None)

    def getcode(self):
        return self.status


@contextlib.contextmanager
def metered(chat_path: str):
    prof = Profile()
    restore = []
    try:
        import ollama
    except ImportError:
        ollama = None
    if ollama is not None and hasattr(ollama, "chat"):
        real_chat = ollama.chat

        def chat(*a, **kw):
            t = time.perf_counter()
            resp = real_chat(*a, **kw)
            prof.record_chat(resp, time.perf_counter() - t)
            return resp
        ollama.chat = chat
        restore.append(lambda: setattr(ollama, "chat", real_chat))

    real_open = urllib.request.urlopen

    def urlopen(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if not url.rstrip("/").endswith(chat_path):
            return real_open(req, *a, **kw)
        t = time.perf_counter()
        with real_open(req, *a, **kw) as r:
            body = r.read()
        try:
            prof.record_chat(json.loads(body.decode("utf-8")), time.perf_counter() - t)
        except (ValueError, UnicodeDecodeError):
            pass
        return _Reply(body, r)
    urllib.request.urlopen = urlopen
    restore.append(lambda: setattr(urllib.request, "urlopen", real_open))
    try:
        yield prof
    finally:
        for undo in reversed(restore):
            undo()


def vram(ps_url: str, model: str, timeout_s: float) -> dict:
    """{"vram_mib", "size_mib"} for `model` from Ollama's /api/ps; None for what it doesn't say."""
    out = {"vram_mib": None, "size_mib": None}
    try:
        with urllib.request.urlopen(ps_url, timeout=timeout_s) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError):
        return out
    for m in data.get("models") or []:
        if model in (m.get("name"), m.get("model")) or (m.get("name") or "").split(":latest")[0] == model:
            mib = 1024 * 1024
            if m.get("size_vram") is not None:
                out["vram_mib"] = round(m["size_vram"] / mib)
            if m.get("size") is not None:
                out["size_mib"] = round(m["size"] / mib)
    return out


def installed(tags_url: str, timeout_s: float) -> set | None:
    """Model names Ollama has (None if it can't be reached). Never pulls."""
    try:
        with urllib.request.urlopen(tags_url, timeout=timeout_s) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    names = set()
    for m in data.get("models") or []:
        for n in (m.get("name"), m.get("model")):
            if n:
                names.add(n)
                names.add(n.removesuffix(":latest"))
    return names
