"""Look things up in Chuen's Obsidian vault, on demand.

This is the second attempt at letting Denisse see the vault. The first one
(2026-09-02) pasted the whole Dev/Memory/ folder into the system prompt and was
measured at 0/4 clean replies against 4/4 with it off -- see the disabled
block in brain.py and the entry in Dev/Memory/decisions.md. The lesson recorded
there is about BUDGET: an 8B model spreads its attention over everything in
the prompt, so content that is usually irrelevant makes every reply worse.

So nothing is injected here. The vault is a tool she calls when a question
is actually about his notes, and the result is capped hard: at most three
notes, a few lines each, around a thousand characters in total. On a
question that isn't about the vault, this module costs one tool
*description* and nothing else.

Keyword search, not embeddings: no model to load, no index to keep in step
with the files, and a wrong answer is visibly a wrong answer. If that turns
out to be the limit, that is the point to consider embeddings -- not before.
"""
import json
import os
import re
import datetime

import brain_rules
import settings

VAULT = settings.vault()  # DENISSE_VAULT, config.json, or Obsidian's own registry

# Every skip list, stop word, weight, cap and sentence lives in the JSON
# beside this file (Rule 0). Read at call time, so editing CONFIG (or the
# file) changes behaviour without touching this code.
with open(os.path.splitext(os.path.abspath(__file__))[0] + ".json", encoding="utf-8-sig") as _config_file:
    CONFIG = json.load(_config_file)


def _msg(key, **fields):
    return CONFIG["messages"][key].format(**fields)


def _stem(w):
    """Crudest possible stem, so a question's plural finds the note's
    singular. 'lectures' -> 'lecture' matches both 'Lectures' and
    'Lecturer', which is the difference between finding the module hub's
    lecturer line and missing it. Everything downstream matches on this as
    a PREFIX, so over-stemming only ever widens the search."""
    swaps = CONFIG["stem_replacements"]
    for suf in CONFIG["stem_suffixes"]:
        if len(w) - len(suf) >= CONFIG["stem_min_root"] and w.endswith(suf):
            return w[: -len(suf)] + swaps.get(suf, "")
    return w


def _words(query):
    out = []
    stop = set(CONFIG["stop_words"])
    for w in re.findall(CONFIG["word_pattern"], (query or "").lower()):
        if len(w) >= CONFIG["min_word_length"] and w not in stop:
            s = _stem(w)
            if s not in out:
                out.append(s)
    return out


def _clean_line(raw):
    """A quotable version of one line: wikilinks reduced to the words a
    person would read, markdown furniture dropped.

    Without this, a week note's file-link lines won -- they are long and
    full of path words like 'Lecture' and 'Tutorial', so a search for
    'lecture' quoted a row of file paths instead of the sentence that
    actually said something.
    """
    line = re.sub(r"\[\[[^\]\|]*\|([^\]]*)\]\]", r"\1", raw)   # [[path|alias]] -> alias
    line = re.sub(r"\[\[([^\]\|]*)\]\]", r"\1", line)          # [[note]] -> note
    line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)       # [text](url) -> text
    line = line.replace("**", "").replace("`", "")
    return line.strip().lstrip("#>-*|[! ").strip()


def _split(text):
    """(frontmatter dict-ish, body) -- the body is what gets quoted."""
    fm = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                k, sep, v = line.partition(":")
                if sep and not line.startswith(" "):
                    fm[k.strip().lower()] = v.strip()
            body = text[end + 4:]
    return fm, body


def _notes():
    for dirpath, dirnames, filenames in os.walk(VAULT):
        dirnames[:] = [d for d in dirnames if d not in CONFIG["skip_dirs"]]
        for fn in filenames:
            if not fn.endswith(CONFIG["note_extension"]):
                continue
            path = os.path.join(dirpath, fn)
            rel = path.replace(VAULT + os.sep, "").replace(os.sep, "/")
            if rel.startswith(tuple(CONFIG["skip_prefixes"])):
                continue
            yield path, rel


def is_overview_query(query):
    text = re.sub(r"[^a-z ]", "", (query or "").lower()).strip()
    return (text in set(CONFIG["overview_exact"])
            or any(re.search(p, text) for p in CONFIG["overview_patterns"]))


def _current_overview(rules):
    """Read dated STATE excerpts on demand; never read the whole vault.

    Heading dates outrank file-level 'updated', which can change after an
    unrelated edit. Cite the actual section date; this is recorded context,
    not a live machine/task-status probe.
    """
    note = CONFIG["state_note"]
    path = os.path.join(VAULT, *note.split("/"))
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read(CONFIG["state_read_chars"])
    except OSError:
        return _msg("overview_unreadable", note=note)
    fm, body = _split(text)
    headings = list(re.finditer(r"(?m)^#{2,4}\s+([^\n]+)", body))
    sections = []
    today = datetime.date.today().isoformat()
    cutoff = (datetime.date.today() - datetime.timedelta(days=rules.context_max_age_days)).isoformat()
    for i, heading in enumerate(headings):
        date = re.search(r"\b\d{4}-\d{2}-\d{2}\b", heading.group(1))
        if not date or not cutoff <= date.group(0) <= today:
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        content = " ".join(_clean_line(line) for line in body[heading.end():end].splitlines()
                           if line.strip() and not line.lstrip().startswith(tuple(CONFIG["overview_skip_starts"])))
        if content:
            sections.append((date.group(0), i, heading.group(1), content))
    sections.sort(reverse=True)
    if not sections:
        return _msg("overview_empty", note=note)
    header = _msg("overview_header")
    budget = rules.vault_max_chars - len(header)
    out = []
    # Fairly share the budget so one long update cannot crowd out the rest.
    selected = sections[:rules.vault_max_notes]
    share = max(1, (budget - 2 * len(selected)) // len(selected))
    for date, _i, heading, content in selected:
        citation = _msg("overview_citation", note=note, heading=heading, date=date)
        block = citation + content[:max(0, share - len(citation))]
        out.append(block[:share])
    return (header + "\n\n".join(out))[:rules.vault_max_chars]


def search_vault(query: str = "") -> str:
    """Search Chuen's Obsidian notes and quote what they say."""
    rules = brain_rules.load()
    if not rules.vault_search or os.environ.get(CONFIG["disable_env"], "").lower() in CONFIG["disable_values"]:
        return _msg("disabled")
    if is_overview_query(query):
        overview = _current_overview(rules)
        if overview.startswith(_msg("overview_header")):
            return overview
        # No state note to quote (a vault without one, 2026-11-04: "what am
        # I working on" got a dead end, and the model answered from its own
        # folder name). His project notes are still there: search them.
        notes = _keyword_search(_words(CONFIG["overview_fallback_query"]), rules)
        if notes.startswith(_msg("hits_header")):
            return overview + "\n\n" + notes
        return overview
    words = _words(query)
    if not words:
        return _msg("nothing_to_search")
    return _keyword_search(words, rules)


def _keyword_search(words: list, rules) -> str:
    """The notes that mention `words`, best first, quoted."""
    if not os.path.isdir(VAULT):
        return _msg("no_vault", vault=VAULT)
    weights = CONFIG["scoring"]
    lcfg = CONFIG["lines"]

    hits = []
    for path, rel in _notes():
        try:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
        except OSError:
            continue
        fm, body = _split(text)
        low_body = body.lower()
        # Every frontmatter value, not a fixed list of keys: module notes
        # carry 'lecturer', 'topic', 'covers' and more, and a question is
        # often about exactly those.
        meta = " ".join(str(v) for v in fm.values()).lower() + " " + rel.lower()

        score = 0
        matched = 0
        for w in words:
            in_meta = meta.count(w)
            in_body = low_body.count(w)
            if in_meta or in_body:
                matched += 1
            # Title and description are the author's own summary, so a hit
            # there says much more than a hit buried in prose.
            score += in_meta * weights["meta_weight"] + min(in_body, weights["body_cap"])
        if not matched:
            continue
        # A note matching every word beats one matching a single word a lot.
        score += matched * weights["matched_bonus"]
        if matched < len(words) and len(words) > 1:
            score -= weights["partial_penalty"]

        # The best lines: whole words only, so 'em' doesn't match 'theme'.
        pats = [re.compile(r"\b%s" % re.escape(w), re.I) for w in words]
        lines = []
        for raw in body.splitlines():
            line = _clean_line(raw)
            if len(line) < lcfg["min_chars"] or line.startswith(tuple(lcfg["skip_starts"])):
                continue
            n = sum(1 for p in pats if p.search(line))
            if n:
                lines.append((n, min(len(line), lcfg["sort_length_cap"]), line))
        # Most query words first, then the FULLER line: the short ones are
        # headings and table cells ('5CCYB010 Electromagnetism'), and
        # preferring them buried the one line that answers the question
        # ('Lecturer: Dr Oleg Aslanidi').
        lines.sort(key=lambda t: (-t[0], -t[1]))
        hits.append((score, rel, fm.get(CONFIG["description_key"], "").strip('"'), lines))

    if not hits:
        # Say what was searched, not just that nothing turned up. On
        # 2026-10-26 this came back empty for "passport" and was relayed
        # to Chuen as the file not existing anywhere -- but it only ever
        # looked at notes, and the file was on disk the whole time.
        return _msg("no_hits", words=_msg("word_join").join(words))

    hits.sort(key=lambda h: -h[0])
    out = []
    used = 0
    for _, rel, desc, lines in hits[:rules.vault_max_notes]:
        chunk = [f"{rel}"]
        if desc:
            chunk.append(_msg("description", description=desc[:CONFIG["description_chars"]]))
        cap = lcfg["max_chars"]
        for _, _, line in lines[:lcfg["per_note"]]:
            chunk.append(_msg("line", line=line[:cap] + lcfg["ellipsis"] if len(line) > cap else line))
        block = "\n".join(chunk)
        if used + len(block) > rules.vault_max_chars and out:
            break
        out.append(block)
        used += len(block)

    return (_msg("hits_header") + "\n\n".join(out))[:rules.vault_max_chars]


if __name__ == "__main__":  # quick manual check
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    for q in sys.argv[1:] or ["electromagnetism lecturer", "who is my signals lecturer",
                              "what did we decide about the reminder card"]:
        print("=" * 70)
        print("Q:", q)
        print(search_vault(q))
