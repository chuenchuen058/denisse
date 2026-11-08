"""Tutor notes in the vault: laid out from the model's lists, written only
over what Denisse herself wrote.

Every line of the note comes from a template in tutor.json ("note"). Links
are only made to things that exist (the week note, the module hub, source
files inside the vault), so scripts/vault_contract.py never finds a broken
one. The frontmatter carries name/description/updated/tags as the contract
asks, with a name slug unique to the module and week.

Overwrite rule (the same as study_brief.py's drafts): the state remembers
the hash of every note she wrote. A note on disk whose hash differs is his
-- edited or written by him -- and is never touched; her new version goes
beside it with a copy suffix and its own name slug.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os

from .conf import CONFIG, Module, tutor_name

_N = CONFIG["note"]
_P = CONFIG["paths"]


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_hash(path: str) -> str:
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            return _hash_text(f.read())
    except OSError:
        return ""


def _rel(vault: str, path: str) -> str:
    return os.path.relpath(path, vault).replace(os.sep, "/")


def _inside(vault: str, path: str) -> bool:
    v = os.path.normcase(os.path.abspath(vault))
    p = os.path.normcase(os.path.abspath(path))
    return p == v or p.startswith(v + os.sep)


def note_path(module: Module, week: int) -> str:
    return os.path.join(module.week_dir(week), _N["file"].format(module=module.title, week=week))


def index_path(module: Module) -> str:
    return os.path.join(module.folder, _N["index_file"].format(module=module.title))


def _links(vault: str, module: Module, week: int | None) -> str:
    links = []
    if week is not None and os.path.isfile(os.path.join(module.week_dir(week),
                                                        _P["week_note"].format(module=module.title, week=week))):
        links.append(_N["week_link"].format(module=module.title, week=week))
    if os.path.isfile(os.path.join(module.folder, _P["hub_note"].format(module=module.title))):
        links.append(_N["hub_link"].format(module=module.title))
    if week is not None:  # ingest writes the index in the same run, after the notes
        links.append(_N["index_link"].format(module=module.title))
    return _N["link_sep"].join(links)


def render(vault: str, module: Module, week: int, result: dict, sources: list,
           today: dt.date, name: str | None = None, provenance: dict | None = None,
           missing: list | None = None, chunks: int = 0) -> str:
    """The whole note. `sources` are full paths of the files it was read from;
    `missing` the chunk numbers (of `chunks`) the model never answered for."""
    name = name or _N["name"].format(slug=module.slug, week=week)
    ctx = {"module": module.title, "code": module.code, "week": week, "slug": module.slug,
           "today": today.isoformat(), "name": name, "tutor": tutor_name()}
    lines = [line.format(**ctx) for line in _N["front"]]
    links = _links(vault, module, week)
    if links:
        lines.append(_N["links_line"].format(links=links))
    lines += [line.format(**ctx) for line in _N["generated_callout"]]
    if missing:
        lines += [line.format(failed=len(missing), chunks=chunks,
                              list=_N["list_sep"].join(str(n) for n in missing)) for line in _N["incomplete_callout"]]
    lines += [_N["sources_heading"], ""]
    for src in sources:
        base = os.path.basename(src)
        if _inside(vault, src):
            lines.append(_N["source_link"].format(path=_rel(vault, src), name=base))
        else:
            lines.append(_N["source_plain"].format(name=base))
        info = (provenance or {}).get(src, {})
        for key, pages in (("note_warning", info.get("ocr_pages", [])),
                           ("incomplete_warning", info.get("pending_pages", []) + info.get("capped_pages", []))):
            if pages:
                lines += ["", CONFIG["extract"]["ocr"][key].format(
                    name=base, pages=_N["list_sep"].join(str(n) for n in sorted(pages))), ""]
    lines += ["", _N["outcomes_heading"], ""]
    lines += [_N["outcome"].format(text=t) for t in result["outcomes"]] or [_N["empty"]]
    lines += ["", _N["methods_heading"], ""]
    lines += [_N["method"].format(
        name=m["name"],
        equation=_N["method_equation"].format(equation=m["equation"]) if m.get("equation") else "",
        use=_N["method_use"].format(use=m["use"]) if m.get("use") else "") for m in result["methods"]] or [_N["empty"]]
    lines += ["", _N["examples_heading"], ""]
    if result["examples"]:
        for n, ex in enumerate(result["examples"], 1):
            lines.append(_N["example"].format(n=n, problem=ex["problem"]))
            lines += [_N["example_step"].format(n=i, text=s) for i, s in enumerate(ex["steps"], 1)]
            lines.append("")
    else:
        lines += [_N["empty"], ""]
    lines += [_N["check_heading"], ""]
    lines += [_N["check"].format(text=t) for t in result["check"]] or [_N["empty"]]
    lines += ["", _N["topics_heading"], ""]
    lines += [_N["topic"].format(
        name=t["name"],
        prerequisites=_N["topic_prerequisites"].format(list=_N["list_sep"].join(t["prerequisites"]))
        if t.get("prerequisites") else "") for t in result["topics"]] or [_N["empty"]]
    return "\n".join(lines).rstrip("\n") + "\n"


def write(path: str, text: str, written: dict) -> tuple:
    """Write `text` at `path` unless a file there isn't hers. Returns
    (path_written, kept_his). `written` maps path -> hash of what she last
    wrote there and is updated in place."""
    kept = False
    if os.path.exists(path) and file_hash(path) != written.get(path):
        stem, ext = os.path.splitext(path)
        copy = stem + _N["copy_suffix"] + ext
        # The copy must not share the original's frontmatter name either.
        text = _rename(text)
        if os.path.exists(copy) and file_hash(copy) != written.get(copy):
            raise FileExistsError(f"{copy} exists and isn't Denisse's either")
        path, kept = copy, True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    written[path] = _hash_text(text)
    return path, kept


def _rename(text: str) -> str:
    """'name: x' in the frontmatter -> 'name: x<copy_name_suffix>'."""
    head, sep, rest = text.partition("\n---")
    if not sep:
        return text
    lines = head.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("name:"):
            lines[i] = line.rstrip() + _N["copy_name_suffix"]
            break
    return "\n".join(lines) + sep + rest


def render_index(vault: str, module: Module, lectures: dict, topic_map: dict, today: dt.date,
                 extra_links: list | None = None) -> str:
    """The module's tutor index: a link per tutor note and the topic map.
    `lectures` is state.lectures[slug]: week -> {note, copy, ...};
    `extra_links` (the tutor's own notes) go beside the module hub link."""
    ctx = {"module": module.title, "code": module.code, "slug": module.slug, "today": today.isoformat(),
           "name": _N["index_name"].format(slug=module.slug), "tutor": tutor_name()}
    hub = _N["hub_link"].format(module=module.title) if os.path.isfile(
        os.path.join(module.folder, _P["hub_note"].format(module=module.title))) else ""
    hub = _N["link_sep"].join([x for x in [hub] + list(extra_links or []) if x])
    lines = [line.format(hub_link=hub, **ctx) for line in _N["index_front"]]
    weeks = sorted(int(w) for w, rec in lectures.items() if rec.get("note"))
    for w in weeks:
        rec = lectures[str(w)]
        copy = ""
        if rec.get("copy"):
            stem = os.path.splitext(os.path.basename(rec["copy"]))[0]
            copy = _N["index_week_copy"].format(copy=stem, tutor=tutor_name())
        lines.append(_N["index_week"].format(module=module.title, week=w, copy=copy))
    if not weeks:
        lines.append(_N["index_none"])
    lines += _N["index_topics_heading"]
    for t in topic_map.get("topics", []):
        lines.append(_N["index_topic"].format(
            name=t["name"], weeks=_N["list_sep"].join(str(w) for w in t["lectures"]),
            prerequisites=_N["index_topic_prerequisites"].format(list=_N["list_sep"].join(t["prerequisites"]))
            if t.get("prerequisites") else "",
            coursework=_N["index_topic_coursework"].format(list=_N["list_sep"].join(t["coursework"]))
            if t.get("coursework") else ""))
    if not topic_map.get("topics"):
        lines.append(_N["empty"])
    return "\n".join(lines).rstrip("\n") + "\n"
