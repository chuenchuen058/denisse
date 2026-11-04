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

Softcoded (Rule 0, 2026-11-03; second pass 2026-11-04): the word lists,
limits and every sentence she says here are in dialogue_rules.json
("clarify"). His answer is read by its words, not
matched against fixed sentences: a short reply made only of agreeing
words and courtesy filler is a yes ("yeah thats it thanks"), of refusing
words and filler a no ("nah neither of them"), and a short reply with a
position or "one"/"other"/"both" in it is a choice. Stretched words and
typos ("yesss", "corect", "secnd") are matched to the nearest word
(dialogue_rules.closest). The choice itself stays in code, not the model:
see above for why.
"""
import os
import re
import threading
import time

import dialogue_rules

_R = dialogue_rules.get("clarify")

EXPIRY_SECONDS = _R["expiry_seconds"]
_STOP = frozenset(_R["stop_words"])
_AFFIRM = frozenset(_R["affirm_words"])
_NEGATE = frozenset(_R["negate_words"])
_FILLER = frozenset(_R["reply_filler"])
_OTHER = frozenset(_R["other_words"])
_SELECT = frozenset(_R["selection_words"])
_ORDINAL = dict(_R["ordinals"])
_NAMED = _R["max_options_named"]
_QUOTED_LABEL = re.compile(r"\s*(?:note\s+)?['\"]([^'\"]{2,%d})['\"]" % _R["quoted_label_chars"], re.I)
_COLON_LABEL = re.compile(r"\s*([^:]{2,%d}?):\s" % _R["colon_label_chars"])

_lock = threading.Lock()
_pending = None


def _words(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower().replace("_", " "))


def _near(word, vocabulary):
    return dialogue_rules.closest(word, vocabulary, _R["fuzzy_ratio"], _R["fuzzy_min_len"])


def _only(text, vocabulary):
    """A short reply made of `vocabulary` words (one at least) and filler."""
    words = _words(text)
    if not words or len(words) > _R["short_reply_words"]:
        return False
    hits = [w for w in words if _near(w, vocabulary)]
    return bool(hits) and all(w in hits or w in _FILLER for w in words)


def _phrase(text):
    """One of the agreeing phrases with no agreeing word in them."""
    flat = " ".join(_words(text.replace("'", "")))
    return any(flat == " ".join(_words(p.replace("'", ""))) for p in _R["affirm_phrases"])


def _is_yes(text):
    return _only(text, _AFFIRM) or _phrase(text)


def _is_no(text):
    return _only(text, _NEGATE)


def _ordinal(word):
    """The index a position word stands for ('secnd' too), or None."""
    hit = _near(word, [k for k in _ORDINAL if not k.isdigit()]) if not word.isdigit() else word
    return _ORDINAL.get(hit) if hit else None


def _is_selection(text):
    """Shaped like an answer to "which one?" -- short, with a position or a
    choosing word in it -- so it can't be a new request."""
    words = _words(text)
    core = [w for w in words if w not in _FILLER and w not in _NEGATE and w not in _SELECT]
    return (0 < len(words) <= _R["short_reply_words"] and len(core) <= _R["selection_core_words"]
            and any(w in _SELECT or _ordinal(w) is not None for w in words))


def _segments(result):
    """(title, text) items in one tool result: vault_search blocks
    ('Folder/Note.md' + '  - line'), list lines, and sentences."""
    items, title = [], ""
    for raw in (result or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(tuple(_R["skip_line_starts"])):
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
    m = _QUOTED_LABEL.match(text)
    if m:
        return m.group(1)
    m = _COLON_LABEL.match(text)
    if m and not re.search(r"\d", m.group(1)):
        return m.group(1).replace("_", " ").strip()
    words = []
    for w in text.rstrip(".!?").split()[:_R["label_max_words"]]:
        if re.search(r"\d", w) or w.lower() in _R["label_stops"]:
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
    return _R["article"] + label


def candidates(reads, terms):
    """Items from the evidence that could be what he meant. `reads` are the
    attempt's read-only calls with their results; `terms` his subject."""
    items = []
    for c in reads:
        for title, text in _segments(str(c.get("result", ""))):
            items.append({"title": title, "text": text, "tool": c["name"], "arguments": c.get("arguments") or {}})
    titles = [i["title"] for i in items if i["title"]]
    subject = [t for t in (terms or "").split() if len(t) >= _R["min_subject_chars"]]
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
    return _R["quote"].format(name=spoken(option), evidence=evidence(option))


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
    names = [_speak(o["label"]) for o in options[:_NAMED]]
    if len(names) == 1:
        return _R["ask_one"].format(name=names[0])
    return _R["ask_many"].format(names=", ".join(names[:-1]), last=names[-1])


def missing(terms, seen):
    """Nothing else in the evidence: say what was found and what's needed."""
    names = [_speak(o["label"]) for o in seen[:_NAMED]]
    if len(names) == 1:
        found = _R["missing_one"].format(terms=terms or _R["missing_one_terms"], name=names[0])
    else:
        found = _R["missing_many"].format(terms=terms or _R["missing_many_terms"],
                                          names=", ".join(names[:-1]), last=names[-1])
    return found + _R["missing_tail"]


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
        n = _ordinal(w)
        if n is not None and (_is_selection(t) or len(_words(t)) <= _R["ordinal_alone_words"]):
            return ("pick", options[n]) if n < len(options) else ("unsupported", None)
    if _is_yes(t) or _only(t, _OTHER):
        return ("pick", options[0]) if len(options) == 1 else ("ambiguous", options)
    if complaint or _is_no(t):
        return "none", None
    if _is_selection(t):
        return "unsupported", None
    return "unrelated", None
