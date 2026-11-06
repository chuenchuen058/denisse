"""python -m tutor ingest: his lecture files into tutor notes and a topic map.

    1. scan     every file in each module's inbox folder and the shared
                inbox folders (tutor.json paths.inboxes); nothing signs in
                to KEATS -- he downloads, this reads. And the lecture files
                he already keeps in the vault, read where they are: each
                module's Week N / Reference folders (paths.in_place)
    2. match    each file to a module and a week: from the folder it is in,
                then its name, then the first page of its text. Nothing is
                asked; a file that can't be placed is recorded as unmatched
                with the reason, and left where it is
    3. dedupe   by content hash: a file already read is not read again, and
                a week whose files haven't changed is not re-extracted
    4. extract  text (extract.py), then the local model's reading of it
                (model.py), chunked to the brain's context
    5. write    the tutor note into the module's Week folder (notes.py),
                never over anything he wrote; the module's topic map
                (topics.py) and tutor index

Files stay where they are -- inbox or vault, never copied, moved or
renamed. A file whose content hash is already in the state is not read
again unless its placement could change. State (what was read, what was written, the hash
of each note she wrote) lives in memory/tutor/ingest.json -- private, never
committed. Everything printed comes from tutor.json "say".
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re

from . import conf, don, extract, model, notes, topics
from .conf import CONFIG, Module, Paths

_MATCH = CONFIG["match"]
_SAY = CONFIG["say"]
_REASONS = _MATCH["unmatched_reasons"]
_WEEK_DIR = re.compile(_MATCH["week_dir"])
_WEEK = [re.compile(p) for p in _MATCH["week"]]
_CODE = re.compile(_MATCH["code"])
_IN_PLACE = CONFIG["paths"]["in_place"]
_IN_PLACE_DIRS = [re.compile(p) for p in _IN_PLACE["folders"]]
_IN_PLACE_FILES = [re.compile(p) for p in _IN_PLACE["files"].values()]
_IN_PLACE_EXT = {e.lower() for e in _IN_PLACE["extensions"]}


# -- state -------------------------------------------------------------------

def load_state(paths: Paths) -> dict:
    """The ingest state: from the vault's Don area, else (not moved yet)
    from the old private place."""
    try:
        with open(paths.readable(CONFIG["paths"]["state_file"]), encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    state.setdefault("files", {})
    state.setdefault("lectures", {})
    state.setdefault("written", {})
    return state


def save_state(paths: Paths, state: dict) -> None:
    os.makedirs(paths.data_dir, exist_ok=True)
    tmp = paths.state_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, paths.state_file())


# -- scanning ----------------------------------------------------------------

def inboxes(paths: Paths, modules: list) -> list:
    """[(folder, module_or_None)] -- every inbox folder that exists."""
    out = [(m.inbox(), m) for m in modules if os.path.isdir(m.inbox())]
    out += [(p, None) for p in paths.shared_inboxes if os.path.isdir(p)]
    return out


def in_place(modules: list) -> list:
    """[(folder, module)] -- each module's own folders (Week N, Reference)
    whose files are read where they are (tutor.json paths.in_place)."""
    out = []
    for m in modules:
        try:
            names = sorted(os.listdir(m.folder))
        except OSError:
            continue
        for name in names:
            full = os.path.join(m.folder, name)
            if os.path.isdir(full) and extract.wanted(name) and any(rx.match(name) for rx in _IN_PLACE_DIRS):
                out.append((full, m))
    return out


def lecture_file(name: str) -> bool:
    """A file in a vault folder that counts as lecture material: one of the
    configured extensions, and a name matching one of the configured kinds."""
    stem, ext = os.path.splitext(name)
    return ext.lower() in _IN_PLACE_EXT and any(rx.search(stem) for rx in _IN_PLACE_FILES)


def scan(folder: str, keep=None) -> list:
    """Every wanted file under `folder`, as (path, relative folder parts).
    `keep(file_name)`, when given, narrows it further."""
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if extract.wanted(d))
        rel = os.path.relpath(dirpath, folder)
        parts = [] if rel == os.curdir else rel.split(os.sep)
        for fn in sorted(filenames):
            if extract.wanted(fn) and (keep is None or keep(fn)):
                found.append((os.path.join(dirpath, fn), parts))
    return found


def files(paths: Paths, modules: list) -> tuple:
    """([(path, folder parts, home module or None)], inbox count, vault
    folder count): his files in the vault first (so a file that is in an
    inbox too is cited where it stays), then the inboxes. In the vault the
    parts start at the module folder, so 'Week 3' places the week. A path
    found twice is listed once."""
    boxes, own = inboxes(paths, modules), in_place(modules)
    out, seen = [], set()
    found = [(p, [os.path.basename(folder)] + parts, home)
             for folder, home in own for p, parts in scan(folder, lecture_file)]
    found += [(p, parts, home) for folder, home in boxes for p, parts in scan(folder)]
    for p, parts, home in found:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            out.append((p, parts, home))
    return out, len(boxes), len(own)


# -- matching ----------------------------------------------------------------

def week_in(text: str, folder: bool = False) -> int | None:
    if folder:
        m = _WEEK_DIR.match(text.strip())
        if m:
            return int(m.group(1))
    for rx in _WEEK:
        m = rx.search(text)
        if m:
            return int(m.group(1))
    return None


def module_in(text: str, modules: list) -> Module | None:
    """The module a folder name, file name or page names: by code first."""
    for code in _CODE.findall(text or ""):
        hit = next((m for m in modules if m.code.lower() == code.lower()), None)
        if hit:
            return hit
    return conf.find_module(text, modules)


def match(path: str, parts: list, home: Module | None, modules: list, text_of) -> tuple:
    """(module, week, why). `text_of(path)` gives the file's text, read only
    when the folder and the name don't settle it."""
    stem = os.path.splitext(os.path.basename(path))[0]
    module = home
    for name in parts if module is None else []:
        module = module_in(name, modules)
        if module:
            break
    if module is None:
        module = module_in(stem, modules)
    week = next((w for w in (week_in(p, folder=True) for p in parts) if w), None)
    if week is None:
        week = week_in(stem)
    if module is None or week is None:
        head = (text_of(path) or "")[:_MATCH["content_chars"]]
        if module is None and head:
            module = module_in(head, modules)
        if week is None and head:
            week = week_in(head)
    if module is None:
        return None, None, _REASONS["module"]
    if week is None:
        return module, None, _REASONS["week"]
    return module, week, ""


def _place(path: str, parts: list, home, modules: list, known: dict | None, read) -> tuple:
    """(module, week, why) for one file; `read(path)` gives (text, why).
    A file whose content was seen before (`known`, by its hash) is not read
    again while its folder and name don't contradict where it was placed
    last time: the same bytes give the same first page."""
    if known:
        quick = match(path, parts, home, modules, lambda _p: "")
        same_module = quick[0] is None or quick[0].slug == known.get("module")
        same_week = quick[1] is None or quick[1] == known.get("week")
        module = next((m for m in modules if m.slug == known.get("module")), None)
        if same_module and same_week and (module is not None or not known.get("module")):
            return module, known.get("week"), known.get("why", "")
    module, week, why = match(path, parts, home, modules, lambda p: read(p)[0])
    if module and week and not read(path)[0]:
        # Placed by its folder or name, but nothing can be read from it.
        module, week, why = None, None, read(path)[1]
    return module, week, why


def _signature(hashes: list) -> str:
    return hashlib.sha256("\n".join(sorted(hashes)).encode("utf-8")).hexdigest()


# -- the job -----------------------------------------------------------------

def run(paths: Paths, call=None, apply: bool = True, today: dt.date | None = None,
        say=print, num_ctx: int | None = None) -> dict:
    """The whole ingest. Returns counts: notes, unchanged, unmatched, failed,
    plus the lists behind them. `call` is the model (tests: a fake);
    `apply=False` scans and matches only."""
    today = today or dt.date.today()
    now = dt.datetime.now().isoformat(timespec="seconds")
    modules = conf.modules(paths)
    state = load_state(paths)
    files_, n_boxes, n_own = files(paths, modules)
    if not n_boxes and not files_:
        say(_SAY["no_inbox"].format(module=CONFIG["paths"]["inboxes"]["module"],
                                    shared=", ".join(paths.shared_inboxes) or "-"))
    texts = {}

    def read(path):
        if path not in texts:
            texts[path] = extract.read(path)
        return texts[path]

    def text_of(path):
        return read(path)[0]

    say(_SAY["scan"].format(n=len(files_), inboxes=n_boxes, folders=n_own))
    groups = {}      # (slug, week) -> {"module", "files": [(path, hash)]}
    unmatched = []
    seen_digests = set()
    for path, parts, home in files_:
        digest = extract.sha256(path)
        if digest in seen_digests:
            continue  # the same file twice (inbox and Week folder): read once
        seen_digests.add(digest)
        known = state["files"].get(digest)
        module, week, why = _place(path, parts, home, modules, known, read)
        rec = {"path": path, "module": module.slug if module else None, "week": week,
               "status": "matched" if module and week else "unmatched", "why": why, "seen": now}
        if known and known.get("status") == "matched" and known.get("module") == rec["module"] \
                and known.get("week") == week:
            say(_SAY["skipped"].format(name=os.path.basename(path)))
        elif rec["status"] == "matched":
            say(_SAY["file"].format(status="matched", module=module.title, week=week, name=os.path.basename(path)))
        else:
            say(_SAY["unmatched"].format(name=os.path.basename(path), why=why))
            unmatched.append(rec)
        state["files"][digest] = rec
        if rec["status"] == "matched":
            g = groups.setdefault((module.slug, week), {"module": module, "files": []})
            g["files"].append((path, digest))

    written, unchanged, failed, changed_modules = [], [], [], {}
    for (slug, week), g in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        module = g["module"]
        lectures = state["lectures"].setdefault(slug, {})
        rec = lectures.get(str(week), {})
        signature = _signature([h for _, h in g["files"]])
        note = notes.note_path(module, week)
        if rec.get("signature") == signature and os.path.exists(rec.get("written") or ""):
            say(_SAY["unchanged"].format(module=module.title, week=week))
            unchanged.append((slug, week))
            continue
        sources = [p for p, _ in g["files"]]
        text = "\n\n".join(CONFIG["extract"]["source_heading"].format(name=os.path.basename(p))
                           + "\n" + text_of(p) for p in sources)
        pieces = model.chunks(text, model.chunk_chars(num_ctx))
        say(_SAY["lecture"].format(module=module.title, week=week, n=len(sources), chars=len(text),
                                   chunks=len(pieces)))
        if not apply:
            say(_SAY["dry"].format(path=note))
            continue
        try:
            result, _n = model.extract_lecture(module.title, module.code, week,
                                               [os.path.basename(p) for p in sources], text,
                                               call=call, num_ctx=num_ctx)
            body = notes.render(paths.vault, module, week, result, sources, today)
            path, kept = notes.write(note, body, state["written"])
        except (model.ModelError, OSError) as exc:
            say(_SAY["failed"].format(module=module.title, week=week, why=exc))
            failed.append((slug, week, str(exc)))
            continue
        say(_SAY["kept"].format(path=note, copy=path) if kept else _SAY["wrote"].format(path=path))
        lectures[str(week)] = {"signature": signature, "note": note, "written": path,
                               "copy": path if kept else None, "files": sources,
                               "topics": result["topics"], "updated": now}
        written.append(path)
        changed_modules[slug] = module
        save_state(paths, state)  # progress survives a crash on the next week

    touched = list(written)
    if apply:
        for slug, module in changed_modules.items():
            lectures = state["lectures"].get(slug, {})
            topic_map = topics.build(module, lectures, today)
            path = topics.save(paths, module, topic_map)
            touched.append(path)
            say(_SAY["topics"].format(path=path))
        # Don's own notes first, so each index can link the hub on its first run.
        touched += don.refresh(paths, state, modules, today, say=say)
        for slug, module in changed_modules.items():
            lectures = state["lectures"].get(slug, {})
            topic_map = topics.load(paths, module) or {}
            extra = [don.hub_link(paths)] if os.path.isfile(don.hub_path(paths)) else []
            index = notes.render_index(paths.vault, module, lectures, topic_map, today, extra)
            try:
                path, _kept = notes.write(notes.index_path(module), index, state["written"])
                touched.append(path)
                say(_SAY["index"].format(path=path))
            except (OSError, FileExistsError) as exc:
                say(_SAY["failed"].format(module=module.title, week="index", why=exc))
        if changed_modules:  # the hub and progress notes can link the new indexes now
            touched += don.refresh(paths, state, modules, today, say=lambda _s: None)
        save_state(paths, state)
        touched.append(paths.state_file())
    else:
        say(_SAY["dry_model"])
    say(_SAY["done"].format(notes=len(written), unchanged=len(unchanged), unmatched=len(unmatched),
                            failed=len(failed)))
    return {"notes": written, "unchanged": unchanged, "unmatched": unmatched, "failed": failed,
            "modules": sorted(changed_modules), "touched": touched if apply else []}
