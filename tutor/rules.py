"""Chuen's rules note for the tutor, read before every job (Chuen, 9 Nov 2026).

The note (tutor.json rules.note, in the Don area) is his to edit. Two kinds
of rule in it:

  judgements   go into the model prompts as the note's own text: block(name)
               is the sections tutor.json rules.prompts gives that prompt.
               Nothing here knows what a rule says.
  machinery    each behind its own tutor.json switch (on(name)):
               exams(...) / scope_weeks(...)   the Exams table: which weeks an
                                               exam ahead covers (anki.apply_scope
                                               suspends the rest, quizzes keep in)
               pending(...) / mark_synced(...) modules whose cards never reached Anki
               batch_lock(...)                 one local-model batch job at a time
               check_kinds(...)                the exam-style card kinds are on
               handoff(...)                    one line per job in the day's Handoff note
               weakest(...)                    the weakest Anki topic in scope

load(paths) reads the note once per change (mtime); a missing or unreadable
note is said once and the defaults apply: no rules text, no exams.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import re
import tempfile
import time

from . import conf
from .conf import CONFIG, Module, Paths

_R = CONFIG["rules"]
_X = _R["exam_scope"]
log = logging.getLogger("denisse.tutor")


@dataclasses.dataclass
class Rules:
    path: str
    found: bool = False
    why: str = ""
    body: str = ""
    sections: dict = dataclasses.field(default_factory=dict)   # heading -> text, in note order
    rows: list = dataclasses.field(default_factory=list)       # the Exams table rows, as dicts


ACTIVE: Rules | None = None      # what the prompts read; set by load()
_cache: dict = {}                 # path -> (mtime, Rules)
_said_missing: set = set()


def on(name: str) -> bool:
    """A machinery rule's switch (default on)."""
    return bool(_R.get(name, {}).get("enabled", True))


def note_path(paths: Paths) -> str:
    from . import don
    return os.path.join(paths.don_dir, _R["note"].format(**don.ctx()))


def _sections(body: str) -> dict:
    heading = re.compile(_R["heading"])
    out, name, buf = {}, "", []
    for line in body.splitlines():
        m = heading.match(line)
        if m:
            if buf and "".join(buf).strip():
                out[name] = "\n".join(buf).strip()
            name, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    if buf and "".join(buf).strip():
        out[name] = "\n".join(buf).strip()
    return out


def _cells(line: str) -> list:
    m = re.match(_R["table_row"], line)
    return [c.strip() for c in m.group(1).split("|")] if m else []


def _tables(body: str) -> list:
    """Every markdown table in the note: [[header cells], [row cells], ...]."""
    out, cur = [], []
    for line in body.splitlines() + [""]:
        cells = _cells(line)
        if cells:
            if not re.match(_R["table_rule"], line):
                cur.append(cells)
        elif cur:
            out.append(cur)
            cur = []
    return out


def _rows(body: str) -> list:
    """The rows of the table whose header has every column tutor.json
    rules.exam_scope.columns names, as {column key: cell}."""
    want = _X["columns"]
    for table in _tables(body):
        header = [conf.words(h) for h in table[0]]
        at = {}
        for key, word in want.items():
            hit = [i for i, h in enumerate(header) if word.lower() in h]
            if hit:
                at[key] = hit[0]
        if len(at) == len(want):
            return [{k: (r[i] if i < len(r) else "") for k, i in at.items()} for r in table[1:]]
    return []


def _strip_tables(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not _cells(line)).strip()


def load(paths: Paths, say=None) -> Rules:
    """Read the rules note (again only when it changed) and make it ACTIVE."""
    global ACTIVE
    path = note_path(paths)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    hit = _cache.get(path)
    if hit and hit[0] == mtime and mtime is not None:
        ACTIVE = hit[1]
        return ACTIVE
    rules = Rules(path=path)
    if mtime is None:
        rules.why = _R["missing_why"]
    else:
        try:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as exc:
            rules.why = str(exc) or type(exc).__name__
        else:
            from . import sources
            _fm, body = sources.frontmatter(text)
            rules.found, rules.body = True, body.strip()
            rules.sections = _sections(rules.body)
            rules.rows = _rows(rules.body)
    if rules.found:
        _said_missing.discard(path)
    elif path not in _said_missing:
        _said_missing.add(path)
        from . import don
        (say or log.info)(_R["missing"].format(path=path, why=rules.why, **don.ctx()))
    _cache[path] = (mtime, rules)
    ACTIVE = rules
    return rules


def reset() -> None:
    """Forget the note (tests)."""
    global ACTIVE
    ACTIVE = None
    _cache.clear()
    _said_missing.clear()


def text_for(name: str, rules: Rules | None = None) -> str:
    """The note's text for one prompt: its configured sections (tables
    left out), else the whole note when a heading isn't there."""
    rules = rules if rules is not None else ACTIVE
    if rules is None or not rules.found:
        return ""
    wanted = _R["prompts"].get(name) or []
    have = {h.casefold(): t for h, t in rules.sections.items()}
    if wanted and all(w.casefold() in have for w in wanted):
        parts = [have[w.casefold()] for w in wanted]
    else:
        parts = [rules.body]
    text = _strip_tables("\n\n".join(parts))
    return text[:_R["prompt_chars"]]


def block(name: str, rules: Rules | None = None) -> str:
    """The prompt block for one prompt ('' without a note)."""
    text = text_for(name, rules)
    if not text:
        return ""
    rules = rules if rules is not None else ACTIVE
    return _R["prompt_block"].format(note=os.path.splitext(os.path.basename(rules.path))[0], rules=text)


# -- exams (rule: respect the exam scope) ------------------------------------------

@dataclasses.dataclass
class Exam:
    name: str
    module_text: str
    module: Module | None
    weeks: list
    date: dt.date | None

    @property
    def slug(self) -> str:
        return conf.slug(self.name)

    @property
    def tag(self) -> str:
        return _X["tag"].format(exam_slug=self.slug)

    def active(self, today: dt.date) -> bool:
        if self.date is None:
            return bool(_X["undated_active"])
        return today <= self.date


def weeks_of(text: str) -> list:
    out = set()
    rest = text or ""
    for a, b in re.findall(_X["week_range"], rest):
        lo, hi = sorted((int(a), int(b)))
        out.update(range(lo, hi + 1))
    rest = re.sub(_X["week_range"], " ", rest)
    out.update(int(n) for n in re.findall(_X["week_number"], rest))
    return sorted(out)


def date_of(text: str) -> dt.date | None:
    for found in re.findall(_X["date_find"], text or ""):
        for fmt in _X["date_formats"]:
            try:
                return dt.datetime.strptime(found.strip(), fmt).date()
            except ValueError:
                continue
    return None


def tag_prefix() -> str:
    """What every exam tag starts with: the tag template up to its first field."""
    return _X["tag"].split("{", 1)[0]


def exams(paths: Paths, rules: Rules | None = None, say=None) -> list:
    """The Exams table as Exam records, modules matched to the vault's."""
    if rules is None:
        rules = ACTIVE if ACTIVE is not None and ACTIVE.path == note_path(paths) else load(paths)
    if not rules.found:
        return []
    modules = conf.modules(paths)
    out = []
    for row in rules.rows:
        name = row.get("exam", "").strip(" *")
        if not name:
            continue
        mtext = row.get("module", "").strip(" *")
        module = conf.find_module(mtext, modules)
        if module is None and say:
            say(_X["say_unknown_module"].format(exam=name, module=mtext))
        out.append(Exam(name, mtext, module, weeks_of(row.get("weeks", "")), date_of(row.get("date", ""))))
    return out


def active_exams(paths: Paths, module: Module, today: dt.date | None = None) -> list:
    if not on("exam_scope"):
        return []
    today = today or dt.date.today()
    return [e for e in exams(paths) if e.module is not None and e.module.slug == module.slug
            and e.weeks and e.active(today)]


def scope_weeks(paths: Paths, module: Module, today: dt.date | None = None) -> set | None:
    """The weeks the module's exams ahead cover, or None when none is set."""
    found = active_exams(paths, module, today)
    if not found:
        return None
    return {w for e in found for w in e.weeks}


def in_scope(card: dict, weeks: set | None) -> bool:
    """A card with no week (his own notes, coursework) is never out of scope."""
    return weeks is None or not card.get("week") or int(card["week"]) in weeks


# -- Anki sync that didn't happen (rule: always reach Anki) ---------------------------

def _sync_file(paths: Paths) -> str:
    return _R["sync_after_cards"]["file"]


def pending(paths: Paths) -> list:
    from . import don
    return list((don.load_json(paths, _sync_file(paths)) or {}).get("modules", []))


def mark_synced(paths: Paths, synced: list, unsynced: list, today: dt.date | None = None) -> str | None:
    """Module slugs synced now leave the list, ones that couldn't be join it.
    Returns the sidecar path when it changed."""
    from . import don
    before = pending(paths)
    after = [s for s in before if s not in set(synced)]
    after += [s for s in unsynced if s not in after]
    if after == before:
        return None
    return don.save_json(paths, _sync_file(paths), {"modules": after,
                                                    "updated": (today or dt.date.today()).isoformat()})


# -- one local-model batch job at a time ------------------------------------------------

class Busy(RuntimeError):
    def __init__(self, line: str):
        super().__init__(line)
        self.line = line


def lock_path(paths: Paths) -> str:
    vault = os.path.normcase(os.path.abspath(paths.vault))
    digest = hashlib.sha256(vault.encode("utf-8")).hexdigest()[:_R["lock"]["hash_chars"]]
    return os.path.join(tempfile.gettempdir(), _R["lock"]["file"].format(vault_hash=digest))


def _alive(pid) -> bool:
    try:
        import psutil
    except ImportError:
        return True  # can't tell: the age decides
    try:
        return psutil.pid_exists(int(pid))
    except (TypeError, ValueError):
        return False


def _holder(path: str) -> dict | None:
    """Who holds the lock, or None when it is free or stale (then removed)."""
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        info = {}
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return None
    if age > _R["lock"]["stale_s"] or not _alive(info.get("pid")):
        with contextlib.suppress(OSError):
            os.remove(path)
        return None
    return info


@contextlib.contextmanager
def batch_lock(paths: Paths, command: str, say=print, wait_s: float | None = None, sleep=time.sleep):
    """Hold the local-model lock for `command`, waiting for another job up
    to wait_s; raises Busy (its one line) when it is still held. A command
    not in rules.lock.commands, or the switch off, takes no lock."""
    c = _R["lock"]
    if not on("lock") or command not in c["commands"]:
        yield None
        return
    from . import don
    path = lock_path(paths)
    wait_s = c["wait_s"] if wait_s is None else wait_s
    waited, told = 0.0, False
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = _holder(path)
            if info is None and not os.path.exists(path):
                continue  # free, or a stale lock just cleared: try again
            info = info or {}
            fields = dict(command=info.get("command", "?"), pid=info.get("pid", "?"), this=command,
                          wait_s=int(wait_s), **don.ctx())
            if waited >= wait_s:
                raise Busy(c["say_busy"].format(**fields))
            if not told:
                say(c["say_wait"].format(**fields))
                told = True
            step = min(c["poll_s"], max(wait_s - waited, 0)) or c["poll_s"]
            sleep(step)
            waited += step
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "command": command,
                       "started": dt.datetime.now().isoformat(timespec="seconds")}, f)
        break
    try:
        yield path
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


# -- exam-style cards every week ---------------------------------------------------------

def check_kinds(say=print) -> list:
    """The exam-style kinds (rules.exam_style_kinds.kinds) that are off in
    cards.kinds, each said once."""
    if not on("exam_style_kinds"):
        return []
    c = _R["exam_style_kinds"]
    kinds = CONFIG["cards"]["kinds"]
    off = [k for k in c["kinds"] if (kinds.get(k) or {}).get("count", 0) <= 0]
    for k in off:
        say(c["say_off"].format(kind=k))
    return off


# -- the day's Handoff line ---------------------------------------------------------------

def handoff(paths: Paths, job: str, say=None, now: dt.datetime | None = None, **fields) -> bool:
    """One line for one job (rules.handoff.commands key) in the vault's
    Handoff note (atrium.handoff)."""
    c = _R["handoff"]
    tpl = c["commands"].get(job)
    if not on("handoff") or not tpl:
        return False
    from . import don
    try:
        from atrium import handoff as writer
        line = tpl.format(**{"command": job, **don.ctx(), **fields})
        return writer.remember(c["agent"].format(**don.ctx()), [line], vault=paths.vault, now=now)
    except Exception as exc:  # noqa: BLE001 -- the job's work stands without its log line
        if say:
            say(CONFIG["say"]["not_committed"].format(why=exc))
        return False


# -- weak topics drive practice -------------------------------------------------------------

def weakest(paths: Paths, module: Module, today: dt.date | None = None) -> str:
    """The weakest Anki topic of the module inside the exam scope ('' if none)."""
    if not on("weak_topics"):
        return ""
    from . import don
    stats = don.load_json(paths, CONFIG["don"]["anki_file"].format(slug=module.slug)) or {}
    weeks = scope_weeks(paths, module, today)
    for t in stats.get("topics", []):  # anki.review_stats sorts weak first, then by score
        if not t.get("weak"):
            continue
        if weeks is None or not t.get("weeks") or set(t["weeks"]) & weeks:
            return t["topic"]
    return ""
