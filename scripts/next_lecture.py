"""Next-lecture prep notes, one per module per week, written by the nightly job.

The adjacent JSON file supplies paths, naming patterns, selection rules,
limits and output templates. It is loaded when this module is imported.
Module titles are discovered from folder names using those patterns; there
is no module-name list. The default vault comes from vault_search's settings.

Each module's newest qualifying week gets topics from its metadata, file
names and linked concept sections, open items from the prior week, and
earlier concepts ranked with vault_search's tokenizer.

Like study_brief.py, nothing here composes content: every line is a file
name, a note title, a heading or a line already in his notes, and the note
says so. A prep note for a module+week that already exists, under any date,
is never overwritten or duplicated.

    python scripts/next_lecture.py --vault <path> [--date YYYY-MM-DD] [--dry]
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import vault_search  # noqa: E402  (its tokenizer; brain_rules is not consulted)

with open(os.path.splitext(__file__)[0] + ".json", encoding="utf-8") as _config_file:
    CONFIG = json.load(_config_file)

_PATTERNS = CONFIG["patterns"]
_NAMING = CONFIG["naming"]
_RULES = CONFIG["week_rules"]
_CONCEPTS = CONFIG["concepts"]
_TEXT = CONFIG["templates"]
VAULT = vault_search.VAULT
MODULES_REL = os.path.join(*CONFIG["paths"]["modules"])
DRAFTS_REL = os.path.join(*CONFIG["paths"]["drafts"])
SOON_DAYS = _RULES["soon_days"]
MAX_TOPICS = CONFIG["limits"]["topics"]
MAX_EARLIER = CONFIG["limits"]["earlier"]

WEEK_DIR = re.compile(_PATTERNS["week_dir"])
_FRONT = re.compile(_PATTERNS["frontmatter"])
_TODO = re.compile(_PATTERNS["todo"])
_LINK = re.compile(_PATTERNS["wikilink"])
_H3 = re.compile(_PATTERNS["concept_heading"])
_H2 = re.compile(_PATTERNS["section_boundary"])
_MODULE_CODE = re.compile(_PATTERNS["module_code"])
_MODULE_SUFFIX = re.compile(_PATTERNS["module_suffix"])
_TOPIC_KINDS = CONFIG["topics"]["file_kinds"]
_GENERIC = re.compile(_PATTERNS["generic_topic"])


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
    """Remove the configured course-code prefix and folder suffix."""
    parts = folder.split(_NAMING["module_separator"], 1)
    name = parts[1] if len(parts) == 2 and _MODULE_CODE.match(parts[0]) else folder
    return _MODULE_SUFFIX.sub("", name).strip()


def _date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat((value or "").strip()[:len(dt.date.min.isoformat())])
    except ValueError:
        return None


def read_weeks(module_dir: str, title: str) -> list:
    """Every configured week folder with a note: number, folder, note, front."""
    weeks = []
    for sub in os.listdir(module_dir):
        m = WEEK_DIR.match(sub)
        note = os.path.join(module_dir, sub, _NAMING["week_note"].format(
            title=title, week=m.group(1))) if m else ""
        if m and os.path.isfile(note):
            weeks.append({"week": int(m.group(1)), "dir": os.path.join(module_dir, sub),
                          "note": note, "front": _front(_read(note))})
    return sorted(weeks, key=lambda w: w["week"])


def pick_week(weeks: list, today: dt.date) -> dict | None:
    """The newest week with a pending status or date in the configured window."""
    def qualifies(w):
        fm = w["front"]
        if fm.get(_RULES["status_field"], "").strip().lower() in _RULES["pending_statuses"]:
            return True
        when = next((date for key in _RULES["date_fields"]
                     if (date := _date(fm.get(key, "")))), None)
        return bool(when and today <= when <= today + dt.timedelta(days=SOON_DAYS))
    hits = [w for w in weeks if qualifies(w)]
    return hits[-1] if hits else None


def file_topics(week_dir: str) -> list:
    """(topic, filename) from lecture/tutorial file names in the folder."""
    out = []
    for fn in sorted(os.listdir(week_dir)):
        stem, _ = os.path.splitext(fn)
        kind, sep, rest = stem.partition(_NAMING["file_topic_separator"])
        if not sep or kind not in _TOPIC_KINDS:
            continue
        rest = rest.strip()
        if rest and not _GENERIC.match(rest):
            out.append((rest, fn))
    return out


def concept_notes(module_dir: str, title: str) -> list:
    """Concept notes the module hub links to, as paths that exist."""
    hub = os.path.join(module_dir, _NAMING["hub_note"].format(title=title))
    if not os.path.isfile(hub):
        return []
    names = {os.path.splitext(os.path.basename(p))[0].lower(): p
             for p in glob.glob(os.path.join(glob.escape(module_dir), _NAMING["notes_glob"]))}
    out = []
    for target in _LINK.findall(_read(hub)):
        stem = os.path.splitext(os.path.basename(target.strip()))[0]
        path = names.get(stem.lower())
        if not path or path in out:
            continue
        fm = _front(_read(path))
        if (stem.lower().endswith(tuple(_CONCEPTS["name_suffixes"]))
                or any(tag in fm.get(_CONCEPTS["tags_field"], "").lower()
                       for tag in _CONCEPTS["tag_fragments"])):
            out.append(path)
    return out


def concept_sections(path: str, title: str) -> list:
    """Each concept section: heading, text and cited weeks of this module."""
    text = _read(path)
    heads = list(_H3.finditer(text))
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        nxt = _H2.search(text[h.end():end])
        body = text[h.end():h.end() + nxt.start()] if nxt else text[h.end():end]
        weeks = sorted({int(n) for n in re.findall(
            _PATTERNS["week_reference"].format(title=re.escape(title)), body)})
        out.append({"heading": h.group(1), "body": body, "weeks": weeks,
                    "note": os.path.splitext(os.path.basename(path))[0]})
    return out


def prior_todos(weeks: list, n: int) -> tuple:
    """(prior week dict or None, [todo callout titles]) using the configured offset."""
    prev = next((w for w in weeks if w["week"] == n - _RULES["prior_week_offset"]), None)
    if not prev:
        return None, []
    return prev, [t for t in _TODO.findall(_read(prev["note"])) if t]


def _score(words: list, text: str) -> int:
    low = text.lower()
    return sum(1 for w in words if re.search(
        _PATTERNS["score_word"].format(word=re.escape(w)), low))


# -- the note --------------------------------------------------------------

def _rel(vault: str, path: str) -> str:
    return os.path.relpath(path, vault).replace(os.sep, "/")


def build(vault: str, module_dir: str, title: str, week: dict, weeks: list,
          today: dt.date) -> str:
    n = week["week"]
    fm = week["front"]
    week_link = _TEXT["week_link"].format(path=_rel(vault, week["note"]), title=title, week=n)
    context = {"date": today.isoformat(), "title": title, "week": n,
               "prior_week": n - _RULES["prior_week_offset"], "week_link": week_link}
    topics, seen = [], set()

    def add(label, source):
        key = label.lower()
        if key not in seen and len(topics) < MAX_TOPICS:
            seen.add(key)
            topics.append(_TEXT["topic_item"].format(label=label, source=source))

    topic_field = CONFIG["topics"]["metadata_field"]
    if fm.get(topic_field):
        add(fm[topic_field], _TEXT["metadata_source"].format(field=topic_field, **context))
    for topic, fn in file_topics(week["dir"]):
        add(topic, _TEXT["file_source"].format(filename=fn))
    sections = [s for p in concept_notes(module_dir, title) for s in concept_sections(p, title)]
    for s in sections:
        if n in s["weeks"]:
            add(s["heading"], _TEXT["concept_source"].format(**s))

    prev, todos = prior_todos(weeks, n)
    words = vault_search._words(" ".join([fm.get(topic_field, "")]
                                         + [t for t, _ in file_topics(week["dir"])]))
    earlier = []
    for s in sections:
        if s["heading"].lower() in seen or not s["weeks"] or min(s["weeks"]) >= n:
            continue
        sc = (_score(words, s["heading"]) * _CONCEPTS["heading_weight"]
              + _score(words, s["body"]))
        if sc:
            earlier.append((-sc, min(s["weeks"]), s))
    earlier.sort(key=lambda e: (e[0], e[1]))

    lines = [line.format(**context) for line in _TEXT["header"]]
    lines += topics or [_TEXT["no_topics"]]
    lines += ["", _TEXT["prior_heading"].format(**context), ""]
    if prev and todos:
        prev_link = _TEXT["week_link"].format(
            path=_rel(vault, prev["note"]), title=title, week=context["prior_week"])
        lines += [_TEXT["todo_item"].format(todo=t, link=prev_link) for t in todos]
    elif not prev:
        lines.append(_TEXT["no_prior_week"].format(**context))
    else:
        lines.append(_TEXT["no_todos"].format(**context))
    lines += ["", _TEXT["earlier_heading"], ""]
    if earlier:
        for _, first, s in earlier[:MAX_EARLIER]:
            lines.append(_TEXT["earlier_item"].format(first=first, **s))
    else:
        lines.append(_TEXT["no_earlier"])
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
        if not os.path.isdir(module_dir) or folder.startswith(tuple(_NAMING["skip_prefixes"])):
            continue
        title = module_title(folder)
        weeks = read_weeks(module_dir, title)
        week = pick_week(weeks, today)
        if not week:
            continue
        stem = _NAMING["prep_stem"].format(title=title, week=week["week"])
        path = os.path.join(drafts, _NAMING["dated_prep"].format(date=today.isoformat(), stem=stem))
        already = glob.glob(os.path.join(glob.escape(drafts),
                                        _NAMING["existing_prep_glob"].format(stem=glob.escape(stem))))
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
        say(_TEXT["log_heading"])
        say(_TEXT["log_failure"].format(error_type=type(exc).__name__, error=exc))
        return "failed"
    fresh = [(s, p) for s, p in results if s != "exists"]
    if not fresh:
        return "exists" if results else "none"
    say(_TEXT["log_heading"])
    for status, path in fresh:
        say(_TEXT["log_status"].format(status=status, path=_rel(vault, path)))
    return "wrote"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", default=VAULT)
    ap.add_argument("--date", type=dt.date.fromisoformat)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    for status, path in run(a.vault, a.date, apply=not a.dry):
        print(_TEXT["cli_status"].format(status=status, path=path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
