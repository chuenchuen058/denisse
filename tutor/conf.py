"""Where the tutor's things are, read from tutor.json and the vault itself.

    conf.CONFIG              the whole tutor.json
    conf.paths()             a Paths: vault, state dir, inbox folders -- the real
                             ones; tests build their own Paths(vault=..., state=...)
    conf.modules(paths)      every module folder in the vault, as Module records
    conf.num_ctx()           the brain's context size (brain.json, env wins)

Modules are discovered from the folder names under paths.modules; nothing
here lists them (AGENTS.md Rule 0). A folder '5CCYB020 Signals and Systems'
gives code 5CCYB020 and title 'Signals and Systems'; '(26~27 SY)' style
suffixes are folder-only, as scripts/next_lecture.py treats them.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

with open(os.path.join(HERE, "tutor.json"), encoding="utf-8-sig") as _config_file:
    CONFIG = json.load(_config_file)

_P = CONFIG["paths"]
_M = CONFIG["modules"]
_CODE = re.compile(_M["code"])
_SUFFIX = re.compile(_M["suffix"])
_SLUG = re.compile(_M["slug"])
_NON_WORD = re.compile(_M["non_word"])


@dataclasses.dataclass
class Paths:
    vault: str
    state: str
    shared_inboxes: list = dataclasses.field(default_factory=list)

    @property
    def modules_dir(self) -> str:
        return os.path.join(self.vault, *_P["modules"])

    def state_file(self) -> str:
        return os.path.join(self.state, _P["state_file"])

    def topics_file(self, slug: str) -> str:
        return os.path.join(self.state, _P["topics_file"].format(slug=slug))


@dataclasses.dataclass
class Module:
    folder: str        # full path of the module folder
    code: str          # '5CCYB020' or ''
    title: str         # 'Signals and Systems'

    @property
    def slug(self) -> str:
        return slug(self.title)

    @property
    def words(self) -> list:
        return [w for w in words(self.title) if w not in set(_M["connectors"])]

    def week_dir(self, week: int) -> str:
        return os.path.join(self.folder, _P["week_dir"].format(week=week))

    def inbox(self) -> str:
        return os.path.join(self.folder, _P["inboxes"]["module"])


def slug(text: str) -> str:
    return _SLUG.sub("-", (text or "").lower()).strip("-")


def words(text: str) -> list:
    return [w for w in _NON_WORD.split((text or "").lower()) if w]


def split_folder(folder: str) -> tuple:
    """'5CCYB070 Computational Methods (26~27 SY)' -> ('5CCYB070', 'Computational Methods')."""
    parts = folder.split(_M["separator"], 1)
    if len(parts) == 2 and _CODE.match(parts[0]):
        code, name = parts
    else:
        code, name = "", folder
    return code, _SUFFIX.sub("", name).strip()


def modules(paths: Paths) -> list:
    """Every module folder in the vault, in folder order. No folder = none."""
    root = paths.modules_dir
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if not os.path.isdir(full) or name.startswith(tuple(_M["skip_prefixes"])):
            continue
        code, title = split_folder(name)
        out.append(Module(full, code, title))
    return out


def find_module(text: str, known: list) -> Module | None:
    """The module `text` names: its code; its slug; a short name made only
    of words from its title ('signals', 'comp' is not); or a longer text
    (a file name, a first page) holding every word of its title. Exactly
    one hit, else None."""
    low = words(text)
    connectors = set(_M["connectors"])
    short = [w for w in low if w not in connectors]
    hits = []
    for m in known:
        if ((m.code and m.code.lower() in low) or slug(text) == m.slug
                or (short and all(w in m.words for w in short))
                or (m.words and all(w in low for w in m.words))):
            hits.append(m)
    return hits[0] if len(hits) == 1 else None


def _expand(path: str, vault: str) -> str:
    import settings
    if "{vault}" in path:
        path = path.replace("{vault}", vault)
    return settings.expand(path)


def paths() -> Paths:
    """The real places: the vault from settings, state under the repo's
    memory/ (or DENISSE_TUTOR_STATE), shared inboxes from tutor.json."""
    import settings
    vault = settings.vault()
    state = os.environ.get(_P["state_env"]) or os.path.join(REPO, *_P["state"])
    shared = [_expand(p, vault) for p in _P["inboxes"]["shared"]]
    return Paths(vault=vault, state=state, shared_inboxes=shared)


def num_ctx() -> int:
    """The context the brain runs with: DENISSE_NUM_CTX, else brain.json's
    limits.num_ctx -- the one knob (AGENTS.md: 8192, measured)."""
    env = os.environ.get(_P["context_env"])
    if env and env.strip().isdigit():
        return int(env)
    with open(os.path.join(REPO, _P["context_file"]), encoding="utf-8-sig") as f:
        node = json.load(f)
    for key in _P["context_key"]:
        node = node[key]
    return int(node)
