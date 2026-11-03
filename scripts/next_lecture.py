"""Next-lecture prep notes, one per module per week, written by the nightly job.

The KEATS weekly pull files each module's week under
`Atlas/Academic/SEM 1 Modules/<module>/Week N/` with a `<Module> Week N.md`
note; study_brief.py briefs a module only when asked. This joins them: for
each module it picks the newest week whose note is still `status: files-only`
(lecture not yet written up) or carries a date (`date:` / `lecture_date:`)
within the next 3 days, and writes

    Denisse/Drafts/<date> <Module> Week N prep.md

with:
  * topics     -- the week note's `topic:`, the lecture/tutorial file names in
                  the week folder, and concept sections (in a concepts note the
                  module hub links to) that already cite this week; 3-5 of them
  * still open -- the prior week note's `> [!todo]` callouts
  * earlier    -- the 2 concept sections from earlier weeks that best match
                  this week's topic words (scored with vault_search's tokenizer)

Like study_brief.py, nothing here composes content: every line is a file
name, a note title, a heading or a line already in his notes, and the note
says so. A prep note for a module+week that already exists, under any date,
is never overwritten or duplicated.

    python scripts/next_lecture.py --vault <path> [--date YYYY-MM-DD] [--dry]
"""
import argparse
import datetime as dt
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import vault_search  # noqa: E402  (its tokenizer; brain_rules is not consulted)

VAULT = r"C:\Users\Xichu\OneDrive - King's College London\Denisse"
MODULES_REL = os.path.join("Atlas", "Academic", "SEM 1 Modules")
DRAFTS_REL = os.path.join("Denisse", "Drafts")
SOON_DAYS = 3
MAX_TOPICS = 5
MAX_EARLIER = 2

WEEK_DIR = re.compile(r"^Week (\d+)$")
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)
_TODO = re.compile(r"^>\s*\[!todo\][+-]?\s*(.*?)\s*$", re.MULTILINE | re.IGNORECASE)
_LINK = re.compile(r"\[\[([^]|#]+)(?:#[^]|]*)?(?:\|[^]]*)?\]\]")
_H3 = re.compile(r"(?m)^###\s+(.+?)\s*$")
# File kinds that say what was taught (nightly.py's KINDS); the rest are
# exercises and solutions, which name a sheet, not a topic.
_TOPIC_KINDS = ("Lecture", "Tutorial", "Notes", "Walkthrough")
# Second halves that name the file, not what's in it.
_GENERIC = re.compile(r"(?i)^(notes|slides|questions|problems|problem sheet|solutions?|"
                      r"answers|handout|worked examples?|tutorial \d+|lecture \d+)\b.*$")


# -- reading ---------------------------------------------------------------

def _read(path: str) -> str:
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def _front(text: str) -> dict:
    m = _FRONT.match(text or "")
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        k, sep, v = line.partition(":")
        if sep and not line.startswith(" "):
            out[k.strip().lower()] = v.strip().strip('"')
    return out


def module_title(folder: str) -> str:
    """'5CCYB070 Computational Methods (26~27 SY)' -> 'Computational Methods'
    (same rule as nightly.py's naming stage)."""
    parts = folder.split(" ", 1)
    name = parts[1] if len(parts) == 2 and re.match(r"^[0-9A-Z]{5,}$", parts[0]) else folder
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()


def _date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat((value or "").strip()[:10])
    except ValueError:
        return None


def read_weeks(module_dir: str, title: str) -> list:
    """Every Week N/ folder that has its week note: number, folder, note, front."""
    weeks = []
    for sub in os.listdir(module_dir):
        m = WEEK_DIR.match(sub)
        note = os.path.join(module_dir, sub, f"{title} Week {m.group(1)}.md") if m else ""
        if m and os.path.isfile(note):
            weeks.append({"week": int(m.group(1)), "dir": os.path.join(module_dir, sub),
                          "note": note, "front": _front(_read(note))})
    return sorted(weeks, key=lambda w: w["week"])


def pick_week(weeks: list, today: dt.date) -> dict | None:
    """The newest week still at files-only, or dated today..today+3."""
    def qualifies(w):
        fm = w["front"]
        if fm.get("status", "").strip().lower() == "files-only":
            return True
        when = _date(fm.get("lecture_date", "")) or _date(fm.get("date", ""))
        return bool(when and today <= when <= today + dt.timedelta(days=SOON_DAYS))
    hits = [w for w in weeks if qualifies(w)]
    return hits[-1] if hits else None


def file_topics(week_dir: str) -> list:
    """(topic, filename) from lecture/tutorial file names in the folder."""
    out = []
    for fn in sorted(os.listdir(week_dir)):
        stem, _ = os.path.splitext(fn)
        kind, sep, rest = stem.partition(" - ")
        if not sep or kind not in _TOPIC_KINDS:
            continue
        rest = rest.strip()
        if rest and not _GENERIC.match(rest):
            out.append((rest, fn))
    return out


def concept_notes(module_dir: str, title: str) -> list:
    """Concept notes the module hub links to, as paths that exist."""
    hub = os.path.join(module_dir, f"{title} notes.md")
    if not os.path.isfile(hub):
        return []
    names = {os.path.splitext(os.path.basename(p))[0].lower(): p
             for p in glob.glob(os.path.join(module_dir, "*.md"))}
    out = []
    for target in _LINK.findall(_read(hub)):
        stem = os.path.splitext(os.path.basename(target.strip()))[0]
        path = names.get(stem.lower())
        if not path or path in out:
            continue
        fm = _front(_read(path))
        if stem.lower().endswith("concepts") or "concept" in fm.get("tags", "").lower():
            out.append(path)
    return out


def concept_sections(path: str, title: str) -> list:
    """Each ### section: heading, its text, and which of this module's weeks it cites."""
    text = _read(path)
    heads = list(_H3.finditer(text))
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        nxt = re.search(r"(?m)^##\s", text[h.end():end])
        body = text[h.end():h.end() + nxt.start()] if nxt else text[h.end():end]
        weeks = sorted({int(n) for n in re.findall(
            rf"\[\[{re.escape(title)} Week (\d+)", body)})
        out.append({"heading": h.group(1), "body": body, "weeks": weeks,
                    "note": os.path.splitext(os.path.basename(path))[0]})
    return out


def prior_todos(weeks: list, n: int) -> tuple:
    """(prior week dict or None, [todo callout titles]) for week n-1."""
    prev = next((w for w in weeks if w["week"] == n - 1), None)
    if not prev:
        return None, []
    return prev, [t for t in _TODO.findall(_read(prev["note"])) if t]


def _score(words: list, text: str) -> int:
    low = text.lower()
    return sum(1 for w in words if re.search(r"\b" + re.escape(w), low))


# -- the note --------------------------------------------------------------

def _rel(vault: str, path: str) -> str:
    return os.path.relpath(path, vault).replace(os.sep, "/")


def build(vault: str, module_dir: str, title: str, week: dict, weeks: list,
          today: dt.date) -> str:
    n = week["week"]
    fm = week["front"]
    week_link = f"[[{_rel(vault, week['note'])}|{title} Week {n}]]"
    topics, seen = [], set()

    def add(label, source):
        key = label.lower()
        if key not in seen and len(topics) < MAX_TOPICS:
            seen.add(key)
            topics.append(f"- **{label}** — {source}")

    if fm.get("topic"):
        add(fm["topic"], f"`topic:` in {week_link}")
    for topic, fn in file_topics(week["dir"]):
        add(topic, f"file name `{fn}`")
    sections = [s for p in concept_notes(module_dir, title) for s in concept_sections(p, title)]
    for s in sections:
        if n in s["weeks"]:
            add(s["heading"], f"[[{s['note']}#{s['heading']}|{s['note']}]] (already written)")

    prev, todos = prior_todos(weeks, n)
    words = vault_search._words(" ".join([fm.get("topic", "")]
                                         + [t for t, _ in file_topics(week["dir"])]))
    earlier = []
    for s in sections:
        if s["heading"].lower() in seen or not s["weeks"] or min(s["weeks"]) >= n:
            continue
        sc = _score(words, s["heading"]) * 3 + _score(words, s["body"])
        if sc:
            earlier.append((-sc, min(s["weeks"]), s))
    earlier.sort(key=lambda e: (e[0], e[1]))

    lines = [
        "---",
        f"name: {today:%Y-%m-%d} {title} Week {n} prep",
        f'description: "Prep for {title} Week {n}: topics from the week\'s file names and notes, '
        f'what is still open from Week {n - 1}, and earlier ideas to have ready. Built by Denisse from titles only."',
        f"updated: {today:%Y-%m-%d}",
        "type: draft",
        "---",
        "",
        f"# {title} Week {n} — prep",
        "",
        "> [!info] Titles only",
        "> Every line below is a file name, a note title, a heading or a line already in "
        "your notes. Nothing here says what the lecture contains — that is for you to write "
        f"up in {week_link}.",
        "",
        "## Topics",
        "",
    ]
    lines += topics or ["- None: the week folder has no lecture or tutorial file with a topic in its name."]
    lines += ["", f"## Still open from Week {n - 1}", ""]
    if prev and todos:
        prev_link = f"[[{_rel(vault, prev['note'])}|{title} Week {n - 1}]]"
        lines += [f"- {t} — {prev_link}" for t in todos]
    elif not prev:
        lines.append(f"- There is no Week {n - 1} note.")
    else:
        lines.append(f"- Nothing marked `[!todo]` in the Week {n - 1} note.")
    lines += ["", "## Earlier ideas to have ready", ""]
    if earlier:
        for _, first, s in earlier[:MAX_EARLIER]:
            lines.append(f"- [[{s['note']}#{s['heading']}|{s['heading']}]] — first met Week {first}")
    else:
        lines.append("- No earlier concept section shares a word with this week's topics.")
    return "\n".join(lines) + "\n"


# -- the job ---------------------------------------------------------------

def run(vault: str = VAULT, today: dt.date | None = None, apply: bool = True) -> list:
    """Write any due prep notes. Returns [(status, path)], status in
    'wrote' / 'would write' / 'exists'. Modules with nothing qualifying are
    left out, so an empty list means nothing to do."""
    today = today or dt.date.today()
    modules = os.path.join(vault, MODULES_REL)
    drafts = os.path.join(vault, DRAFTS_REL)
    out = []
    if not os.path.isdir(modules):
        return out
    for folder in sorted(os.listdir(modules)):
        module_dir = os.path.join(modules, folder)
        if not os.path.isdir(module_dir) or folder.startswith("."):
            continue
        title = module_title(folder)
        weeks = read_weeks(module_dir, title)
        week = pick_week(weeks, today)
        if not week:
            continue
        stem = f"{title} Week {week['week']} prep.md"
        path = os.path.join(drafts, f"{today:%Y-%m-%d} {stem}")
        already = glob.glob(os.path.join(glob.escape(drafts), f"* {glob.escape(stem)}"))
        if already:
            out.append(("exists", already[0]))
            continue
        if not apply:
            out.append(("would write", path))
            continue
        text = build(vault, module_dir, title, week, weeks, today)
        os.makedirs(drafts, exist_ok=True)
        try:
            with open(path, "x", encoding="utf-8") as f:  # "x": never overwrite
                f.write(text)
        except FileExistsError:
            out.append(("exists", path))
            continue
        out.append(("wrote", path))
    return out


def nightly_step(vault: str, say, apply: bool = True, today: dt.date | None = None) -> str:
    """The nightly stage. Silent when nothing qualifies; never raises, so a
    failure here can't stop the rest of the nightly. Returns
    'none' / 'wrote' / 'exists' / 'failed'."""
    try:
        results = run(vault, today, apply)
    except Exception as exc:  # noqa: BLE001 -- logged, never fatal
        say("== prep ==")
        say(f"  next_lecture failed: {type(exc).__name__}: {exc}")
        return "failed"
    fresh = [(s, p) for s, p in results if s != "exists"]
    if not fresh:
        return "exists" if results else "none"
    say("== prep ==")
    for status, path in fresh:
        say(f"  {status:<12}{_rel(vault, path)}")
    return "wrote"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", default=VAULT)
    ap.add_argument("--date", type=dt.date.fromisoformat)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    for status, path in run(a.vault, a.date, apply=not a.dry):
        print(f"{status:<12}{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
