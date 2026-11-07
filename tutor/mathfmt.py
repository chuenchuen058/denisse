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
_DELIMITED = [(spec[0], re.compile(spec[1], re.S),
               re.compile(spec[2]) if len(spec) > 2 else None)
              for spec in _M["delimited"]]
_BARE_SIGN = re.compile(_M["bare"]["sign"])
_OPERATOR = re.compile(_M["bare"]["operator"])
_OPERAND = re.compile(_M["bare"]["operand"])
_TRAIL = _M["bare"]["trailing"]
_TOKEN = re.compile(_M["bare"]["token"])
_BRACES = _M["bare"]["braces"]


def _depth(s: str) -> int:
    """Open braces left unclosed in s (escaped \\{ \\} don't count)."""
    for escaped in _BRACES["escaped"]:
        s = s.replace(escaped, "")
    return s.count(_BRACES["open"]) - s.count(_BRACES["close"])


def _bare(text: str) -> list:
    """(start, end) spans of undelimited maths in plain text."""
    tokens = []
    for m in _TOKEN.finditer(text):
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
    pieces, pos = [], 0
    while pos < len(text):
        # Consume the next complete span before looking inside it. Config order
        # breaks ties (display before inline), not precedence across the text.
        found = []
        for priority, (kind, rx, reject) in enumerate(_DELIMITED):
            match = rx.search(text, pos)
            while match and reject and reject.search(match.group("body")):
                # A currency dollar must not consume the next formula's opener.
                match = rx.search(text, match.start() + 1)
            if match:
                found.append((match.start(), priority, kind, match))
        if not found:
            break
        start, _, kind, match = min(found)
        pieces.extend(_plain(text[pos:start]))
        pieces.append((kind, match.group("body").strip()))
        pos = match.end()
    pieces.extend(_plain(text[pos:]))
    out, previous = [], None
    for kind, body in pieces:
        if not body:
            continue
        if kind:
            if previous:
                # Keep adjacent target delimiters from forming a display opener.
                out.append(_M["adjacent_separator"])
            a, b = wrap[kind]
            out.append(f"{a}{body}{b}")
        else:
            out.append(body)
        previous = kind
    return "".join(out)


def _plain(text: str) -> list:
    """Prose and bare-maths pieces, to wrap only after all spans are known."""
    out, pos = [], 0
    for s, e in _bare(text):
        out.append((None, text[pos:s]))
        out.append(("inline", text[s:e]))
        pos = e
    out.append((None, text[pos:]))
    return out
