"""Don's area in the vault: its memory, and Chuen's progress, as notes.

Chuen (5 Nov 2026): "Don should also have access to Obsidian, collect the
files from there and add my progress and memory back into it." So what the
foundation kept under memory/tutor/ (which files were read, the topic maps)
and what this part adds (cards, Anki review stats, weak topics) live in the
vault, in the area tutor.json "don" names:

    <don folder>/<tutor>.md                     the hub: one line per module
    <don folder>/<tutor> - <module> progress.md material read, cards, Anki, weak topics
    <don folder>/<tutor> - <module> cards.md    every card, under a link to its source
    <don folder>/<data>/*.json                  sidecars a note can't hold

    migrate(paths)           copy anything still in the old place into the vault, once
    write_note(...)          write a note, never over one he edited (notes.write's rule)
    refresh(paths, state)    re-render the hub and every module's progress note
    feed_learner(...)        what the review stats say, into learner/ as measured beliefs
    commit(paths, files, ..) every vault write, through scripts/vault_git.py

Every line comes from tutor.json; nothing here names a module, a path or Don.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys

from . import conf, notes
from .conf import CONFIG, Module, Paths

_D = CONFIG["don"]
_P = CONFIG["paths"]
_SAY = CONFIG["say"]
_G = CONFIG["git"]
_LR = CONFIG["learner"]


def ctx(module: Module | None = None, **more) -> dict:
    """The names every template may use."""
    out = {"tutor": conf.tutor_name(), "tutor_slug": conf.tutor_slug(), "data": _D["data"]}
    if module is not None:
        out.update(module=module.title, code=module.code, slug=module.slug)
    out.update(more)
    return out


# -- sidecars ----------------------------------------------------------------

def load_json(paths: Paths, name: str, default=None):
    try:
        with open(paths.readable(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(paths: Paths, name: str, data) -> str:
    path = paths.data(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def migrate(paths: Paths, say=print) -> list:
    """Copy every sidecar still only in the old place (paths.state) into the
    vault. One-time by construction: a file already in the vault is never
    copied over. The old copy is left where it was."""
    moved = []
    if not os.path.isdir(paths.state) or os.path.normcase(os.path.abspath(paths.state)) == \
            os.path.normcase(os.path.abspath(paths.data_dir)):
        return moved
    for name in sorted(os.listdir(paths.state)):
        old = os.path.join(paths.state, name)
        new = paths.data(name)
        if not os.path.isfile(old) or name.endswith(".tmp") or os.path.exists(new):
            continue
        os.makedirs(paths.data_dir, exist_ok=True)
        shutil.copy2(old, new)
        moved.append(new)
        say(_SAY["migrated"].format(name=name, old=old, new=new))
    return moved


# -- notes -------------------------------------------------------------------

def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def hub_path(paths: Paths) -> str:
    return os.path.join(paths.don_dir, _D["hub_file"].format(**ctx()))


def progress_path(paths: Paths, module: Module) -> str:
    return os.path.join(paths.don_dir, _D["progress_file"].format(**ctx(module)))


def cards_path(paths: Paths, module: Module) -> str:
    return os.path.join(paths.don_dir, _D["cards_file"].format(**ctx(module)))


def hub_link(paths: Paths) -> str:
    return _D["hub_link"].format(stem=_stem(hub_path(paths)), **ctx())


def write_note(path: str, text: str, state: dict, say=print) -> str | None:
    """notes.write's rule: a note whose hash isn't what Don last wrote is
    his, and Don's version goes beside it. Returns the path written, or
    None when even the copy beside it is his (said, never overwritten)."""
    try:
        written, _kept = notes.write(path, text, state["written"])
    except (OSError, FileExistsError) as exc:
        say(_SAY["failed"].format(module=_stem(path), week="-", why=exc))
        return None
    return written


def _module_notes(paths: Paths, module: Module) -> list:
    """(path, link template key) for every Don note a module can have."""
    from . import exam, teach  # part 3; imported here so don.py stays importable first
    return [(progress_path(paths, module), "progress_link"), (cards_path(paths, module), "cards_link"),
            (teach.lessons_path(paths, module), "lessons_link"), (exam.note_path(paths, module), "practice_link")]


def _links(paths: Paths, module: Module, own: str) -> str:
    """Links from one of a module's Don notes to the others that exist."""
    out = []
    index = notes.index_path(module)
    if os.path.isfile(index):
        out.append(_D["index_link"].format(stem=_stem(index)))
    for path, tpl in _module_notes(paths, module):
        if path != own and os.path.isfile(path):
            out.append(_D[tpl].format(stem=_stem(path)))
    out.append(hub_link(paths))
    return CONFIG["note"]["link_sep"].join(out)


def _practice_lines(paths: Paths, module: Module) -> list:
    """The progress note's lessons-and-practice section (part 3)."""
    from . import exam, teach
    c = ctx(module)
    lines = []
    lessons = (load_json(paths, teach.sidecar(module)) or {}).get("lessons", [])
    if lessons:
        last = lessons[-1]
        lines.append(_D["progress_lessons"].format(n=len(lessons), topic=last["topic"], date=last["date"], **c))
    practice = load_json(paths, exam.sidecar(module)) or {}
    exams = [h for h in practice.get("history", []) if h.get("kind") == exam.KIND_EXAM and h.get("total")]
    if exams:
        last = exams[-1]
        mean = round(100 * sum(h["marks"] / h["total"] for h in exams) / len(exams))
        lines.append(_D["progress_exams"].format(n=len(exams), mean=mean, marks=last["marks"], total=last["total"],
                                                 title=last["title"], date=last["date"], **c))
    quizzes = [h for h in practice.get("history", []) if h.get("kind") == exam.KIND_QUIZ]
    if quizzes:
        last = quizzes[-1]
        lines.append(_D["progress_quizzes"].format(n=len(quizzes), right=last["right"], asked=last["asked"],
                                                   answered=last.get("answered", last["asked"]), date=last["date"], **c))
    return lines or [_D["progress_practice_none"].format(**c)]


def render_progress(paths: Paths, module: Module, state: dict, today: dt.date) -> str:
    c = ctx(module, today=today.isoformat(), name=_D["progress_name"].format(**ctx(module)))
    own = progress_path(paths, module)
    lines = [line.format(links=_links(paths, module, own), **c) for line in _D["progress_front"]]
    lectures = state["lectures"].get(module.slug, {})
    files = sum(1 for f in state["files"].values() if f.get("module") == module.slug and f.get("status") == "matched")
    weeks = sorted(int(w) for w, rec in lectures.items() if rec.get("note"))
    lines += [_D["progress_material_heading"], ""]
    if weeks:
        lines.append(_D["progress_material"].format(files=files, weeks=CONFIG["note"]["list_sep"].join(map(str, weeks))))
    else:
        lines.append(_D["progress_material_none"])
    topic_map = load_json(paths, _P["topics_file"].format(slug=module.slug)) or {}
    if topic_map.get("topics"):
        lines.append(_D["progress_topics"].format(n=len(topic_map["topics"])))
    lines += ["", _D["progress_cards_heading"], ""]
    deck = load_json(paths, _D["card_file"].format(slug=module.slug)) or {}
    cards = [card for src in deck.get("sources", {}).values() for card in src.get("cards", [])]
    if cards:
        kinds = {}
        for card in cards:
            kinds[card["kind"]] = kinds.get(card["kind"], 0) + 1
        lines.append(_D["progress_cards"].format(
            n=len(cards), sources=sum(1 for s in deck["sources"].values() if s.get("cards")),
            kinds=CONFIG["note"]["list_sep"].join(_D["progress_kind"].format(n=n, kind=k) for k, n in kinds.items()),
            date=deck.get("updated", "")))
    else:
        lines.append(_D["progress_cards_none"].format(module=module.title))
    lines += ["", _D["progress_anki_heading"], ""]
    stats = load_json(paths, _D["anki_file"].format(slug=module.slug)) or {}
    if not stats.get("topics"):
        lines.append(_D["progress_anki_none"])
    else:
        lines.append(_D["progress_anki_synced"].format(date=stats.get("synced", ""), cards=stats.get("cards", 0),
                                                       reviewed=stats.get("reviewed", 0)))
        lines += _D["progress_table"]
        for t in stats["topics"]:
            lines.append(_D["progress_row"].format(
                topic=t["topic"], cards=t["cards"], reviewed=t["reviewed"], lapses=t["lapses"],
                ease=t["ease"] if t.get("ease") is not None else _D["progress_no_ease"], due=t["due"],
                weak=_D["progress_weak_mark"] if t.get("weak") else _D["progress_ok_mark"]))
    lines += ["", _D["progress_weak_heading"], ""]
    weak = [t for t in stats.get("topics", []) if t.get("weak")]
    for t in weak:
        week_links = []
        for w in t.get("weeks", []):
            note = notes.note_path(module, w)
            if os.path.isfile(note):
                week_links.append(_D["progress_weak_week_link"].format(stem=_stem(note), week=w))
        lines.append(_D["progress_weak"].format(
            topic=t["topic"],
            weeks=_D["progress_weak_weeks"].format(links=CONFIG["note"]["list_sep"].join(week_links))
            if week_links else ""))
    if not weak:
        lines.append(_D["progress_weak_none"])
    lines += ["", _D["progress_practice_heading"], ""] + _practice_lines(paths, module)
    return "\n".join(lines).rstrip("\n") + "\n"


def has_data(paths: Paths, module: Module, state: dict) -> bool:
    from . import exam, teach
    files = [_D[k].format(slug=module.slug) for k in ("card_file", "anki_file")]
    files += [teach.sidecar(module), exam.sidecar(module)]
    return bool(state["lectures"].get(module.slug)) or any(os.path.exists(paths.readable(f)) for f in files)


def render_hub(paths: Paths, modules: list, today: dt.date) -> str:
    c = ctx(today=today.isoformat(), name=_D["hub_name"].format(**ctx()))
    lines = [line.format(**c) for line in _D["hub_front"]]
    any_ = False
    for module in modules:
        links = []
        index = notes.index_path(module)
        if os.path.isfile(index):
            links.append(_D["index_link"].format(stem=_stem(index)))
        for path, tpl in _module_notes(paths, module):
            if os.path.isfile(path):
                links.append(_D[tpl].format(stem=_stem(path)))
        if links:
            any_ = True
            lines.append(_D["hub_module"].format(module=module.title, links=CONFIG["note"]["link_sep"].join(links)))
    if not any_:
        lines.append(_D["hub_none"])
    return "\n".join(lines).rstrip("\n") + "\n"


def refresh(paths: Paths, state: dict, modules: list | None = None, today: dt.date | None = None,
            say=print) -> list:
    """Re-render every module's progress note (modules Don knows anything
    about) and then the hub. Returns the paths written."""
    today = today or dt.date.today()
    modules = conf.modules(paths) if modules is None else modules
    out = []
    for module in modules:
        if has_data(paths, module, state):
            path = write_note(progress_path(paths, module), render_progress(paths, module, state, today), state, say)
            if path:
                say(_SAY["progress"].format(path=path))
                out.append(path)
    path = write_note(hub_path(paths), render_hub(paths, modules, today), state, say)
    return out + ([path] if path else [])


# -- the learner model ---------------------------------------------------------

def feed_learner(module: Module, topics: list, today: dt.date, say=print, core=None) -> list:
    """File what the review stats say through learner/'s API: a weak topic
    is a measured 'struggles' belief with the numbers as evidence; a topic
    that was weak and isn't any more supersedes it. Never edits his notes.
    Returns the learner files written (store, note)."""
    if not _LR["enabled"]:
        return []
    if core is None:
        from learner import core
    data = core.load()
    changed = False
    for t in topics:
        c = ctx(module, topic=t["topic"])
        weak_claim, ok_claim = _LR["weak_claim"].format(**c), _LR["ok_claim"].format(**c)
        current = {b["claim"].lower(): b for b in data["beliefs"] if b.get("status") == "current"}
        evidence = _LR["evidence"].format(date=today.isoformat(), lapses=t["lapses"], reviewed=t["reviewed"],
                                          ease=t["ease"] if t.get("ease") is not None else "-", **c)
        if t.get("weak") and weak_claim.lower() not in current:
            if ok_claim.lower() in current:
                belief = core.supersede(data, current[ok_claim.lower()]["id"], weak_claim, "measured", evidence)
            else:
                belief = core.add_belief(data, weak_claim, _LR["area"], "measured", evidence)
            say(_SAY["learner"].format(claim=belief["claim"]))
            changed = True
        elif not t.get("weak") and t.get("reviewed") and weak_claim.lower() in current:
            belief = core.supersede(data, current[weak_claim.lower()]["id"], ok_claim, "measured", evidence)
            say(_SAY["learner"].format(claim=belief["claim"]))
            changed = True
    if not changed:
        return []
    core.save(data)
    return [str(core.STORE), str(core.write_note(data))]


# -- git ---------------------------------------------------------------------

def commit(paths: Paths, files: list, what: str, module: Module | None = None, say=print) -> str | None:
    """Commit exactly `files` (the ones Don wrote) in the vault's own git
    repo through scripts/vault_git.commit_owned. Files outside the vault or
    under never_dirs are left out; no repo at the vault root, no commit."""
    if not _G["enabled"] or not files:
        return None
    vault = os.path.abspath(paths.vault)
    if not os.path.exists(os.path.join(vault, _G["repo_marker"])):
        say(_SAY["not_committed"].format(why=_SAY["no_repo"]))
        return None
    rels = []
    for f in files:
        full = os.path.abspath(f)
        if not notes._inside(vault, full) or not os.path.exists(full):
            continue
        rel = os.path.relpath(full, vault).replace(os.sep, "/")
        if rel.split("/")[0] in _G["never_dirs"] or rel in rels:
            continue
        rels.append(rel)
    if not rels:
        return None
    scripts = os.path.join(conf.REPO, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import vault_git
    try:
        sha = vault_git.commit_owned(vault, rels, _G["messages"][what].format(**ctx(module)))
    except vault_git.BackupError as exc:
        say(_SAY["not_committed"].format(why=exc))
        return None
    if sha:
        say(_SAY["committed"].format(sha=sha[:8], n=len(rels)))
    return sha
