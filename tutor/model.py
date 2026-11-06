"""The local model reads a lecture and says what it teaches.

One call per chunk: a long lecture is cut to fit the brain's context
(conf.num_ctx, less a reserve for the prompt and the answer), each chunk is
asked for the same JSON shape (tutor.json prompt.schema), and the parts are
merged. The model is Ollama's chat model from config.json (model_config);
`extract_lecture(..., call=fake)` swaps the call for a function
`fake(system, user) -> str`, which is how the tests run without Ollama.

Nothing here composes prose: the model's lists are kept as lists, capped
(tutor.json limits), deduplicated, and handed to notes.py to lay out.
"""
from __future__ import annotations

import json
import re

from . import conf
from .conf import CONFIG

_M = CONFIG["model"]
_PR = CONFIG["prompt"]
_L = CONFIG["limits"]
_FENCE = re.compile(_PR["fence"])
_BAD_ESCAPE = re.compile(_PR["bad_escape"])

EMPTY = {"outcomes": [], "methods": [], "examples": [], "check": [], "topics": []}


class ModelError(RuntimeError):
    pass


# -- chunking ----------------------------------------------------------------

def chunk_chars(num_ctx: int | None = None) -> int:
    ctx = conf.num_ctx() if num_ctx is None else num_ctx
    return max(1, int((ctx - _M["reserve_tokens"]) * _M["chars_per_token"]))


def chunks(text: str, limit: int) -> list:
    """Pieces of at most `limit` chars, cut at blank lines, then lines, then
    hard. Never empty: one piece for short text."""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        if len(para) > limit:  # one paragraph bigger than a chunk: by lines
            pieces = [para[i:i + limit] for i in range(0, len(para), limit)] if "\n" not in para \
                else _lines(para, limit)
        else:
            pieces = [para]
        for piece in pieces:
            if buf and len(buf) + 2 + len(piece) > limit:
                out.append(buf)
                buf = piece
            else:
                buf = piece if not buf else buf + "\n\n" + piece
    if buf:
        out.append(buf)
    return out


def _lines(para: str, limit: int) -> list:
    out, buf = [], ""
    for line in para.splitlines():
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        if buf and len(buf) + 1 + len(line) > limit:
            out.append(buf)
            buf = line
        else:
            buf = line if not buf else buf + "\n" + line
    if buf:
        out.append(buf)
    return out


# -- the call ----------------------------------------------------------------

def ollama_call(system: str, user: str) -> str:
    import ollama
    import model_config
    r = ollama.chat(model=model_config.model(_M["config_entry"]), keep_alive=_M["keep_alive"],
                    options={"temperature": _M["temperature"], "num_ctx": conf.num_ctx()},
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    msg = r["message"] if isinstance(r, dict) else getattr(r, "message", None)
    content = msg["content"] if isinstance(msg, dict) else getattr(msg, "content", "")
    return content or ""


def _close(text: str) -> str:
    """The closers a reply that stopped early left off: a lesson or an exam
    question (2026-11-05, seen live) ended on `}]` with the outer object's
    `}` missing. Brackets opened outside strings are closed in reverse."""
    stack, in_str, esc = [], False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    return text + ("\"" if in_str else "") + "".join(reversed(stack))


def _loads(text: str):
    """json.loads, then once more with the model's LaTeX backslashes
    (`\\pi`, `\\frac`) escaped -- a backslash that starts no JSON escape is
    prompt.bad_escape -- and the closers it left off put back."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    fixed = _BAD_ESCAPE.sub(r"\\\\", text)
    for candidate in (fixed, _close(fixed)):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def parse(reply: str) -> dict | None:
    """The JSON object in a reply, fences and prose around it tolerated,
    LaTeX inside it and a missing closing brace repaired. None when there
    isn't one."""
    text = (reply or "").strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    start, end = text.find(_PR["json_start"]), text.rfind(_PR["json_end"])
    if start == -1:
        return None
    end = len(text) - 1 if end < start else end
    data = _loads(text[start:end + 1])
    if data is None and end + 1 < len(text):
        data = _loads(text[start:])
    return data if isinstance(data, dict) else None


def _clip(text) -> str:
    text = " ".join(str(text or "").split())
    return text[:_L["item_chars"]]


def normalise(data: dict) -> dict:
    """The model's answer in the one shape notes.py reads: strings clipped,
    wrong types dropped, lists capped."""
    out = {k: [] for k in EMPTY}
    for item in (data.get("outcomes") or [])[:_L["outcomes"]]:
        if _clip(item):
            out["outcomes"].append(_clip(item))
    for item in (data.get("methods") or [])[:_L["methods"]]:
        if isinstance(item, dict) and _clip(item.get("name")):
            out["methods"].append({"name": _clip(item.get("name")), "equation": _clip(item.get("equation")),
                                   "use": _clip(item.get("use"))})
        elif isinstance(item, str) and _clip(item):
            out["methods"].append({"name": _clip(item), "equation": "", "use": ""})
    for item in (data.get("examples") or [])[:_L["examples"]]:
        if isinstance(item, dict) and _clip(item.get("problem")):
            steps = item.get("steps") or []
            if isinstance(steps, str):
                steps = [steps]
            out["examples"].append({"problem": _clip(item["problem"]),
                                    "steps": [_clip(s) for s in steps[:_L["example_steps"]] if _clip(s)]})
        elif isinstance(item, str) and _clip(item):
            out["examples"].append({"problem": _clip(item), "steps": []})
    for item in (data.get("check") or [])[:_L["check"]]:
        if _clip(item):
            out["check"].append(_clip(item))
    for item in (data.get("topics") or [])[:_L["topics"]]:
        if isinstance(item, dict) and _clip(item.get("name")):
            pre = item.get("prerequisites") or []
            if isinstance(pre, str):
                pre = [pre]
            out["topics"].append({"name": _clip(item["name"]),
                                  "prerequisites": [_clip(p) for p in pre[:_L["prerequisites"]] if _clip(p)]})
        elif isinstance(item, str) and _clip(item):
            out["topics"].append({"name": _clip(item), "prerequisites": []})
    return out


def merge(parts: list) -> dict:
    """Several chunks' answers as one, in order, without repeats, capped."""
    out = {k: [] for k in EMPTY}
    seen = {k: set() for k in EMPTY}

    def key(item):
        return (item["name"] if isinstance(item, dict) and "name" in item
                else item["problem"] if isinstance(item, dict) else item).lower()

    for part in parts:
        for k in EMPTY:
            for item in part.get(k, []):
                if key(item) not in seen[k] and len(out[k]) < _L[k]:
                    seen[k].add(key(item))
                    out[k].append(item)
    return out


def extract_lecture(module: str, code: str, week: int, sources: list, text: str,
                    call=None, num_ctx: int | None = None, say=None) -> tuple:
    """(result, chunk_count). Raises ModelError when a chunk never gives
    JSON, so a note is never written from a half-read lecture."""
    call = call or ollama_call
    pieces = chunks(text, chunk_chars(num_ctx))
    parts = []
    for n, piece in enumerate(pieces, 1):
        user = _PR["user"].format(module=module, code=code or "", week=week, sources="; ".join(sources),
                                  part=n, parts=len(pieces), schema=_PR["schema"], text=piece)
        data = None
        for attempt in range(1 + _M["json_retries"]):
            reply = call(_PR["system"], user if attempt == 0 else user + _PR["retry"])
            data = parse(reply)
            if data is not None:
                break
        if data is None:
            raise ModelError(f"chunk {n} of {len(pieces)}: the model did not answer with JSON")
        parts.append(normalise(data))
        if say:
            say(n, len(pieces))
    return merge(parts), len(pieces)
