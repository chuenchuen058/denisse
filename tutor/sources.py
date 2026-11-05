"""A module's material in the vault: every note Don may make cards from.

Chuen (5 Nov 2026): Don reads the whole vault, not just the KEATS inbox.
A module's sources are

  * every note in its folder, at any depth: his own notes, Reference,
    Coursework, Week N, the study briefs, the tutor notes ingest wrote; and
  * any other vault note whose frontmatter names the module (tutor.json
    sources.frontmatter_keys: its code, slug or title) or which links to it
    (sources.link_names: its hub note, its title, its folder name).

Skipped: folders in sources.exclude_dirs, Don's own area, the tutor index
and '(tutor update)' copies (the same material twice), and notes shorter
than sources.min_chars once the frontmatter is gone. Each source keeps its
vault-relative path, so a card can cite it as a vault link.
"""
from __future__ import annotations

import dataclasses
import os
import re

from . import conf, notes
from .conf import CONFIG, Module, Paths

_S = CONFIG["sources"]
_N = CONFIG["note"]
_P = CONFIG["paths"]
_LINK = re.compile(r"\[\[([^\]|#]+)")
_WEEK_DIR = re.compile(CONFIG["match"]["week_dir"])


@dataclasses.dataclass
class Source:
    path: str          # full path
    rel: str           # vault-relative, '/'-separated
    kind: str          # sources.tutor (written by ingest) or sources.own (his)
    week: int | None
    topic: str
    text: str          # body, frontmatter removed

    @property
    def stem(self) -> str:
        return os.path.splitext(os.path.basename(self.path))[0]

    @property
    def link(self) -> str:
        return _S["source_link"].format(rel=os.path.splitext(self.rel)[0], stem=self.stem)


def frontmatter(text: str) -> tuple:
    """(flat key -> value dict, body). Not YAML: the vault uses flat keys."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm = {}
    for line in text[3:end].splitlines():
        if not line.strip() or line.startswith((" ", "#")):
            continue
        key, sep, value = line.partition(":")
        if sep:
            fm[key.strip()] = value.strip().strip("\"'")
    body = text[end + 4:]
    return fm, body.split("\n", 1)[1] if "\n" in body else ""


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _names(module: Module) -> set:
    folder = os.path.basename(module.folder)
    hub = os.path.splitext(_P["hub_note"].format(module=module.title))[0]
    return {n.format(hub_stem=hub, module=module.title, folder=folder).lower() for n in _S["link_names"]}


def _names_module(fm: dict, module: Module) -> bool:
    """A frontmatter value naming the module: its code, its slug, or its title."""
    want = {w for w in (module.code.lower(), module.slug) if w}
    for key in _S["frontmatter_keys"]:
        value = fm.get(key, "")
        if not value:
            continue
        if module.title.lower() in value.lower():
            return True
        items = {conf.slug(v) for v in re.split(r"[\[\],]", value) if v.strip()}
        if items & want:
            return True
    return False


def _links_module(body: str, names: set) -> bool:
    for m in _LINK.finditer(body):
        target = m.group(1).strip().split("/")[-1]
        if target.lower().endswith(_S["note_ext"]):
            target = target[:-len(_S["note_ext"])]
        if target.lower() in names:
            return True
    return False


def _week(fm: dict, rel_parts: list) -> int | None:
    value = fm.get("week", "")
    if value.isdigit():
        return int(value)
    for part in reversed(rel_parts):
        m = _WEEK_DIR.match(part)
        if m:
            return int(m.group(1))
    return None


def _skip_file(path: str, module: Module | None) -> bool:
    """A '(tutor update)' copy, or the module's own tutor index: the same
    material as another source, or only links."""
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem.endswith(_N["copy_suffix"]):
        return True
    return bool(_S["skip_generated_index"] and module is not None
                and os.path.normcase(path) == os.path.normcase(notes.index_path(module)))


def _walk(root: str, skip: list):
    skip_n = [os.path.normcase(os.path.abspath(s)) for s in skip]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _S["exclude_dirs"]
                             and os.path.normcase(os.path.abspath(os.path.join(dirpath, d))) not in skip_n)
        for fn in sorted(filenames):
            if fn.lower().endswith(_S["note_ext"]):
                yield os.path.join(dirpath, fn)


def collect(paths: Paths, module: Module) -> list:
    """Every source note for `module`, its own folder first, then the rest
    of the vault in path order."""
    vault = paths.vault
    tutor_mark = _S["generated_marker"]
    out, seen = [], set()

    def add(path: str, fm: dict, body: str):
        rel = os.path.relpath(path, vault).replace(os.sep, "/")
        if rel in seen or len(body.strip()) < _S["min_chars"]:
            return
        seen.add(rel)
        kind = _S["tutor"] if fm.get(tutor_mark[0]) == tutor_mark[1] else _S["own"]
        out.append(Source(path=path, rel=rel, kind=kind, week=_week(fm, rel.split("/")[:-1]),
                          topic=fm.get("topic", ""), text=body.strip()))

    for path in _walk(module.folder, [paths.don_dir]):
        if _skip_file(path, module):
            continue
        fm, body = frontmatter(_read(path))
        add(path, fm, body)
    names = _names(module)
    for path in _walk(vault, [paths.don_dir, paths.modules_dir]):
        if _skip_file(path, None):
            continue
        fm, body = frontmatter(_read(path))
        if _names_module(fm, module) or _links_module(body, names):
            add(path, fm, body)
    # Other modules' folders: only a note that names this module in its frontmatter.
    for other in conf.modules(paths):
        if other.folder == module.folder:
            continue
        for path in _walk(other.folder, [paths.don_dir]):
            fm, body = frontmatter(_read(path))
            if _names_module(fm, module) and not _names_module(fm, other):
                add(path, fm, body)
    return out

