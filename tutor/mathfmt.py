"""Make the maths in a card render where it is shown.

Models write LaTeX every which way: `$...$`, `$$...$$`, `\\(...\\)`, `\\[...\\]`, or bare
(`The solution is \\mathbf{E} = \\frac{1}{4\\pi\\varepsilon_0}...`). Anki renders only `\\(` `\\[`,
Obsidian only `$` `$$`. `normalise(text, target)` finds every piece of maths, delimited or
bare, and re-wraps it in the delimiters tutor.json "math.targets.<target>" names.
Every pattern and delimiter is in tutor.json "math" (Rule 0).
"""
from __future__ import annotations

import re

from .conf import CONFIG

_M = CONFIG["math"]
_DELIMITED = [(kind, re.compile(rx, re.S)) for kind, rx in _M["delimited"]]
_BARE_SIGN = re.compile(_M["bare"]["sign"])
_OPERATOR = re.compile(_M["bare"]["operator"])
_OPERAND = re.compile(_M["bare"]["operand"])
_TRAIL = _M["bare"]["trailing"]


def _depth(s: str) -> int:
    """Open braces left unclosed in s (escaped \\{ \\} don't count)."""
    s = s.replace("\\{", "").replace("\\}", "")
    return s.count("{") - s.count("}")


def _bare(text: str) -> list:
    """(start, end) spans of undelimited maths in plain text."""
    tokens = []
    for m in re.finditer(r"\S+", text):
        # a space inside braces ("{\partial x}") belongs to the same piece of maths: join until they balance
        if tokens and _depth(tokens[-1][2]) > 0:
            s, _, _ = tokens.pop()
            tokens.append((s, m.end(), text[s:m.end()]))
        else:
            tokens.append((m.start(), m.end(), m.group()))
    spans, i = [], 0
    while i < len(tokens):
        if not _BARE_SIGN.search(tokens[i][2]):
            i += 1
            continue
        start = i
        # pull in a left-hand side like "E =" or "F = q" just before the first command
        while start >= 1 and _OPERATOR.fullmatch(tokens[start - 1][2]):
            start -= 1
            if start >= 1 and _OPERAND.fullmatch(tokens[start - 1][2]):
                start -= 1
        end = i
        # run on while tokens stay mathematical: commands, operators, or short operands between them
        j = i + 1
        while j < len(tokens):
            tok = tokens[j][2]
            if _BARE_SIGN.search(tok) or _OPERATOR.fullmatch(tok):
                end = j
            elif _OPERAND.fullmatch(tok.rstrip(_TRAIL)) and j + 1 < len(tokens) and (
                    _BARE_SIGN.search(tokens[j + 1][2]) or _OPERATOR.fullmatch(tokens[j + 1][2])):
                end = j
            elif _OPERAND.fullmatch(tok.rstrip(_TRAIL)) and _OPERATOR.fullmatch(tokens[j - 1][2]):
                end = j
            else:
                break
            j += 1
        s, e = tokens[start][0], tokens[end][1]
        while e > s and text[e - 1] in _TRAIL:
            e -= 1
        spans.append((s, e))
        i = end + 1
    return spans


def normalise(text: str, target: str) -> str:
    """`text` with every piece of maths wrapped for `target` (a key of math.targets)."""
    if not text:
        return text or ""
    wrap = _M["targets"][target]
    out, pos = [], 0
    found = []
    for kind, rx in _DELIMITED:
        for m in rx.finditer(text):
            if not any(m.start() < e and s < m.end() for s, e, _, _ in found):
                found.append((m.start(), m.end(), kind, m.group("body")))
    found.sort()
    for s, e, kind, body in found:
        out.append(_plain(text[pos:s], wrap))
        a, b = wrap[kind]
        out.append(f"{a}{body.strip()}{b}")
        pos = e
    out.append(_plain(text[pos:], wrap))
    return "".join(out)


def _plain(text: str, wrap: dict) -> str:
    out, pos = [], 0
    a, b = wrap["inline"]
    for s, e in _bare(text):
        out.append(text[pos:s])
        out.append(f"{a}{text[s:e]}{b}")
        pos = e
    out.append(text[pos:])
    return "".join(out)
