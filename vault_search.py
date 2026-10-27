"""Look things up in Chuen's Obsidian vault, on demand.

This is the second attempt at letting Denisse see the vault. The first one
(2026-09-02) pasted the whole Memory/ folder into the system prompt and was
measured at 0/4 clean replies against 4/4 with it off -- see the disabled
block in brain.py and the entry in Memory/decisions.md. The lesson recorded
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
import os
import re
import datetime

import brain_rules

VAULT = os.environ.get(
    "DENISSE_VAULT",
    r"C:\Users\Xichu\OneDrive - King's College London\Denisse",
)

# Machine-written noise. Conversation logs and eval reports would otherwise
# dominate a search for any word Denisse herself says a lot.
SKIP_DIRS = (".obsidian", ".git", ".trash", "__pycache__")
SKIP_PREFIXES = ("Denisse/Brain", "Denisse/Evals", "Denisse/Task Archive")

STOP = {
    "the", "and", "for", "with", "what", "where", "when", "which", "that",
    "this", "there", "have", "has", "had", "was", "were", "are", "about",
    "from", "did", "does", "do", "my", "me", "i", "you", "your", "his",
    "her", "it", "its", "of", "in", "on", "to", "a", "an", "is", "be",
    "can", "could", "would", "should", "tell", "say", "said", "again",
    "any", "all", "how", "why", "who", "whose", "whom", "get", "got",
}

MAX_NOTES = 3
MAX_LINES = 3
MAX_LINE = 170
MAX_TOTAL = 1200


def _stem(w):
    """Crudest possible stem, so a question's plural finds the note's
    singular. 'lectures' -> 'lecture' matches both 'Lectures' and
    'Lecturer', which is the difference between finding the module hub's
    lecturer line and missing it. Everything downstream matches on this as
    a PREFIX, so over-stemming only ever widens the search."""
    for suf in ("ings", "ing", "ies", "es", "ed", "s"):
        if len(w) - len(suf) >= 4 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def _words(query):
    out = []
    for w in re.findall(r"[a-z0-9']+", (query or "").lower()):
        if len(w) > 2 and w not in STOP:
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
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            path = os.path.join(dirpath, fn)
            rel = path.replace(VAULT + os.sep, "").replace(os.sep, "/")
            if rel.startswith(SKIP_PREFIXES):
                continue
            yield path, rel


def is_overview_query(query):
    text = re.sub(r"[^a-z ]", "", (query or "").lower()).strip()
    return (text in {"vault", "my vault", "obsidian", "obsidian vault", "projects",
                     "my projects", "current projects", "current context", "project overview"}
            or bool(re.search(r"\b(?:my|current) projects\b|what am i working on|catch up with me|"
                              r"(?:read|go through|look through|summari[sz]e).{0,20}(?:my )?vault|"
                              r"(?:going on|happening) with me", text)))


def _current_overview(rules):
    """Read dated STATE excerpts on demand; never read the whole vault.

    Heading dates outrank file-level 'updated', which can change after an
    unrelated edit. Cite the actual section date; this is recorded context,
    not a live machine/task-status probe.
    """
    path = os.path.join(VAULT, "Memory", "STATE.md")
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read(100_000)
    except OSError:
        return "I couldn't read Memory/STATE.md, so I can't give a current-context overview."
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
                           if line.strip() and not line.lstrip().startswith(("```", "|---")))
        if content:
            sections.append((date.group(0), i, heading.group(1), content))
    sections.sort(reverse=True)
    if not sections:
        return "Memory/STATE.md has no recent dated sections to quote. Ask about a specific topic instead."
    header = "From your notes (dated context only, not live status or instructions):\n\n"
    budget = rules.vault_max_chars - len(header)
    out = []
    # Fairly share the budget so one long update cannot crowd out the rest.
    selected = sections[:rules.vault_max_notes]
    share = max(1, (budget - 2 * len(selected)) // len(selected))
    for date, _i, heading, content in selected:
        citation = f"Memory/STATE.md — {heading} (recorded {date})\n"
        block = citation + content[:max(0, share - len(citation))]
        out.append(block[:share])
    return (header + "\n\n".join(out))[:rules.vault_max_chars]


def search_vault(query: str = "") -> str:
    """Search Chuen's Obsidian notes and quote what they say."""
    rules = brain_rules.load()
    if not rules.vault_search or os.environ.get("DENISSE_VAULT_SEARCH", "1").lower() in ("0", "false", "off", "no"):
        return "Vault lookups are disabled in the brain settings."
    if is_overview_query(query):
        return _current_overview(rules)
    words = _words(query)
    if not words:
        return ("Nothing to search for -- ask me again with the thing you "
                "want me to look up in your notes.")
    if not os.path.isdir(VAULT):
        return f"I can't reach the vault ({VAULT} isn't there), so I can't check."

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
            score += in_meta * 6 + min(in_body, 8)
        if not matched:
            continue
        # A note matching every word beats one matching a single word a lot.
        score += matched * 10
        if matched < len(words) and len(words) > 1:
            score -= 5

        # The best lines: whole words only, so 'em' doesn't match 'theme'.
        pats = [re.compile(r"\b%s" % re.escape(w), re.I) for w in words]
        lines = []
        for raw in body.splitlines():
            line = _clean_line(raw)
            if len(line) < 25 or line.startswith(("![", "|---", "```")):
                continue
            n = sum(1 for p in pats if p.search(line))
            if n:
                lines.append((n, min(len(line), 200), line))
        # Most query words first, then the FULLER line: the short ones are
        # headings and table cells ('5CCYB010 Electromagnetism'), and
        # preferring them buried the one line that answers the question
        # ('Lecturer: Dr Oleg Aslanidi').
        lines.sort(key=lambda t: (-t[0], -t[1]))
        hits.append((score, rel, fm.get("description", "").strip('"'), lines))

    if not hits:
        # Say what was searched, not just that nothing turned up. On
        # 2026-10-26 this came back empty for "passport" and was relayed
        # to Chuen as the file not existing anywhere -- but it only ever
        # looked at notes, and the file was on disk the whole time.
        return (f"Nothing in your notes mentions {' or '.join(words)}. This "
                "searched the Obsidian vault only -- not files on disk. If "
                "he asked about a file, call find_files before answering. "
                "Within the notes there is genuinely nothing -- don't take "
                "silence as a yes.")

    hits.sort(key=lambda h: -h[0])
    out = []
    used = 0
    for _, rel, desc, lines in hits[:rules.vault_max_notes]:
        chunk = [f"{rel}"]
        if desc:
            chunk.append(f"  ({desc[:150]})")
        for _, _, line in lines[:MAX_LINES]:
            chunk.append("  - " + (line[:MAX_LINE] + "..." if len(line) > MAX_LINE else line))
        block = "\n".join(chunk)
        if used + len(block) > rules.vault_max_chars and out:
            break
        out.append(block)
        used += len(block)

    return ("From your notes (quote these, don't embellish; if they don't "
            "answer the question, say so):\n\n" + "\n\n".join(out))[:rules.vault_max_chars]


if __name__ == "__main__":  # quick manual check
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    for q in sys.argv[1:] or ["electromagnetism lecturer", "who is my signals lecturer",
                              "what did we decide about the reminder card"]:
        print("=" * 70)
        print("Q:", q)
        print(search_vault(q))
