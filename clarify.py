"""He rejected an answer, and the evidence holds more than one: ask which.

2026-10-30. A read found two plausible answers (a sponsor meeting and a
supervisor meeting); she gave one, he said "that's not what I meant", and
she gave the same one again, 0/5 live. Two repairs through the model had
already failed: naming other tools in the refusal, and handing the same
evidence and the rejected answer back to it. So the choice is no longer
the model's. This picks the alternatives out of the evidence that was
already read, works out which one the rejected answer used, asks one
question naming the others, and answers from the one he picks. Every label
it says is a substring of the evidence; nothing is invented.

The open question is process memory, like the one Brain per process
(conversation.get_brain). It only answers the message right after it: the
brain passes her previous reply, which must be that exact question. Any
other message closes it, it expires, and it is taken once.
"""
import os
import re
import threading
import time

EXPIRY_SECONDS = 600
_STOP = {"the", "a", "an", "and", "or", "of", "on", "in", "at", "to", "for", "with", "is", "are", "was", "my",
         "your", "you", "i", "me", "it", "that", "this", "one", "ones", "mean", "meant", "about", "from", "by",
         "do", "did", "what", "which", "when", "where", "no", "not", "yes", "yeah", "yep", "please", "just",
         "s", "m", "pm", "am", "note", "notes", "said", "says"}
_YES = re.compile(r"^\s*(?:yes|yeah|yep|yup|ya|correct|right|exactly|that one|that's the one|that'?s it)"
                  r"(?:[,.!]?\s*(?:please|thanks))?[.!\s]*$", re.I)
_NO = re.compile(r"^\s*(?:no|nope|nah|neither|none of (?:them|those|these)|neither of (?:them|those))"
                 r"(?:[,.!]?\s*(?:not that one|neither))?[.!\s]*$", re.I)
_ORDINAL = {"first": 0, "1": 0, "1st": 0, "second": 1, "2": 1, "2nd": 1, "third": 2, "3": 2, "3rd": 2,
            "fourth": 3, "4": 3, "4th": 3}
# Shaped like an answer to "which one?" -- so it can't be a new request.
_SELECTION = re.compile(r"^\s*(?:no[,.]?\s+)?(?:the\s+)?(?:\w+\s+){0,2}(?:one|other)\b|^\s*(?:the\s+)?(?:last|other)\b"
                        r"|^\s*(?:\d|first|second|third|fourth)\b|^\s*both\b", re.I)

_lock = threading.Lock()
_pending = None


def _words(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower().replace("_", " "))


def _segments(result):
    """(title, text) items in one tool result: vault_search blocks
    ('Folder/Note.md' + '  - line'), list lines, and sentences."""
    items, title = [], ""
    for raw in (result or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("From your notes"):
            continue
        if not raw[:1].isspace() and line.lower().endswith(".md"):
            title = os.path.splitext(os.path.basename(line.replace("\\", "/")))[0]
            continue
        if line.startswith("(") and line.endswith(")"):
            continue  # a note's description line
        line = re.sub(r"^(?:[-*]|\d+[.)])\s+", "", line)
        for part in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line):
            if part.strip():
                items.append((title, part.strip()))
    return items


def _label(title, text, titles, subject=()):
    """A short name for an item, copied from the evidence -- never its date."""
    m = re.match(r"\s*(?:note\s+)?['\"]([^'\"]{2,60})['\"]", text, re.I)
    if m:
        return m.group(1)
    m = re.match(r"\s*([^:]{2,40}?):\s", text)
    if m and not re.search(r"\d", m.group(1)):
        return m.group(1).replace("_", " ").strip()
    words = []
    for w in text.rstrip(".!?").split()[:6]:
        if re.search(r"\d", w) or w.lower() in ("on", "at", "is", "was", "due"):
            break
        words.append(w.rstrip(",;"))
    lead = " ".join(words)
    if lead and any(t in lead.lower() for t in subject):
        return lead  # "Dentist check-up", not the note it sits in
    if title and titles.count(title) == 1 and any(t in title.lower() for t in subject):
        return title
    return lead or title or " ".join(text.split()[:3])


def _speak(label):
    """'Supervisor meeting' -> 'the supervisor meeting'; keeps acronyms and names."""
    first = label.split()[0]
    if len(first) > 1 and first[0].isupper() and first[1:].islower():
        label = label[0].lower() + label[1:]
    return "the " + label


def candidates(reads, terms):
    """Items from the evidence that could be what he meant. `reads` are the
    attempt's read-only calls with their results; `terms` his subject."""
    items = []
    for c in reads:
        for title, text in _segments(str(c.get("result", ""))):
            items.append({"title": title, "text": text, "tool": c["name"], "arguments": c.get("arguments") or {}})
    titles = [i["title"] for i in items if i["title"]]
    subject = [t for t in (terms or "").split() if len(t) > 2]
    for i in items:
        i["label"] = _label(i["title"], i["text"], titles, subject)
    relevant = [i for i in items if any(t in (i["label"] + " " + i["text"]).lower().replace("_", " ")
                                        for t in subject)]
    # The tool answered his query; if none of its items repeats his words
    # ("fly" vs "Flight to Paris"), all of them are the candidates -- but
    # say so: then only the rejected answer's own words can justify asking.
    if relevant:
        return relevant, True
    return (items if len(reads) == 1 else []), False


def _distinct(options):
    """Each option's own words: not shared by every option, not filler."""
    sets = [set(_words(o["label"] + " " + o["text"])) - _STOP for o in options]
    common = set.intersection(*sets) if len(sets) > 1 else set()
    return [s - common for s in sets]


def rejected(cands, wrong_reply):
    """The candidates the rejected answer was about: the clear best overlap
    of its own words with the reply, or none if that can't be told."""
    if not cands or not wrong_reply:
        return []
    said = set(_words(wrong_reply))
    scores = [len(d & said) for d in _distinct(cands)]
    best = max(scores)
    if best == 0 or scores.count(best) > 1:
        return []
    return [cands[scores.index(best)]]


def spoken(option):
    return _speak(option["label"])


def evidence(option):
    text = option["text"]
    return text if option["label"].lower() in text.lower() else f"{option['label']}: {text}"


def quote(option):
    """The fallback answer: the evidence itself, nothing added."""
    return f"Here's what I found for {spoken(option)}: {evidence(option)}"


def supported(reply, option, seen):
    """A model answer is kept only if every number in it is in the chosen
    item, it names something of that item, and nothing only the others have."""
    if not reply:
        return False
    said, own = set(_words(reply)), set(_words(evidence(option)))
    if any(w not in own for w in said if any(ch.isdigit() for ch in w)):
        return False
    others = set().union(*[set(_words(evidence(o))) for o in seen if o is not option]) - own - _STOP         if len(seen) > 1 else set()
    mine = own - _STOP - set().union(*[set(_words(evidence(o))) for o in seen if o is not option])
    return bool(said & (mine or own - _STOP)) and not (said & others)


def question(options):
    names = [_speak(o["label"]) for o in options[:4]]
    if len(names) == 1:
        return f"Do you mean {names[0]}?"
    return "Which one do you mean: " + ", ".join(names[:-1]) + f" or {names[-1]}?"


def missing(terms, seen):
    """Nothing else in the evidence: say what was found and what's needed."""
    names = [_speak(o["label"]) for o in seen[:4]]
    if len(names) == 1:
        found = f"The only {terms or 'match'} I found is {names[0]}, and that isn't the one you mean."
    else:
        found = (f"For {terms or 'that'} I found only " + ", ".join(names[:-1]) + f" and {names[-1]}, "
                 "and none of those is the one you mean.")
    return found + " I haven't found another, and I won't guess -- what is it called, or where did you note it?"


def offer(request, asked, options, seen, terms):
    """Hold the question open for the next message only."""
    global _pending
    with _lock:
        _pending = {"request": request, "question": asked, "options": options, "seen": seen,
                    "terms": terms, "at": time.monotonic()}


def is_open(asked):
    """Whether `asked` is the question now waiting for his answer."""
    with _lock:
        return bool(_pending) and _pending["question"] == asked


def take(last_said):
    """The open question if `last_said` (her previous reply) is it, else
    None. Taken either way: one message answers it, or closes it."""
    global _pending
    with _lock:
        p, _pending = _pending, None
    if not p or time.monotonic() - p["at"] > EXPIRY_SECONDS:
        return None
    if " ".join((last_said or "").split()) != " ".join(p["question"].split()):
        return None
    return p


def pick(text, p, complaint=False):
    """('pick', option) | ('ambiguous', options) | ('none', None) |
    ('unsupported', None) | ('unrelated', None)."""
    options = p["options"]
    t = (text or "").strip().lower().rstrip(".!?")
    said = set(_words(t)) - _STOP
    distinct = _distinct(p["seen"]) if len(p["seen"]) > 1 else [set(_words(o["label"])) - _STOP for o in p["seen"]]
    own = {id(o): d for o, d in zip(p["seen"], distinct)}
    # Any item he names counts, even the one he first turned down: "the
    # sponsor one" after "do you mean the supervisor meeting?" is a choice.
    hits = [o for o in p["seen"] if said & own.get(id(o), set())] or         [o for o in options if said & own.get(id(o), set())]
    if len(hits) == 1:
        return "pick", hits[0]
    if len(hits) > 1:
        return "ambiguous", hits
    for w in _words(t):
        if w in _ORDINAL and (_SELECTION.match(t) or len(_words(t)) <= 3):
            n = _ORDINAL[w]
            return ("pick", options[n]) if n < len(options) else ("unsupported", None)
    if _YES.match(t) or re.fullmatch(r"(?:the\s+)?other(?:\s+one)?", t):
        return ("pick", options[0]) if len(options) == 1 else ("ambiguous", options)
    if complaint or _NO.match(t):
        return "none", None
    if _SELECTION.match(t):
        return "unsupported", None
    return "unrelated", None
