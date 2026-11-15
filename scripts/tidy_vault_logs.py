"""Tidy the vault's machine-written logs into month folders, and undo it.

    .venv\\Scripts\\python.exe scripts\\tidy_vault_logs.py --dry-run [--root]   # the plan; nothing changes
    .venv\\Scripts\\python.exe scripts\\tidy_vault_logs.py [--root]             # do it, write a manifest
    .venv\\Scripts\\python.exe scripts\\tidy_vault_logs.py --undo <manifest>     # put it all back

  * Calendar/Chats/<file> and Calendar/Sessions/<file> move into <folder>/<YYYY-MM>/ by
    the date the file name starts with, else the note's frontmatter date, else its mtime.
  * Chat notes named after the AGENTS.md / instructions preamble are renamed
    "<date> <source> <title>" with the importer's own title rule (atrium.import_chats
    .ask_title), and their name/H1 lines follow. Names stay unique; nothing is overwritten.
  * Every explicit-path [[wikilink]] and relative/absolute markdown link in the vault that
    points at a moved file is rewritten; bare [[name]] links are left (Obsidian resolves
    them by name) unless the name itself changed. The importer's and capture's state files
    get their stored paths updated, so nothing is imported twice or re-created flat.
  * --root also moves the root files listed in the config (shipped: STATE NOW.md) into
    Denisse/; CLAUDE.md and GEMINI.md stay (the config says why).

No deletions. The real run holds the vault lock (orchestra/vaultlock.py), writes the
manifest (old -> new, and a backup of every note it edits) under .review/ before it
touches anything, and --undo restores from it: a note edited again since is left alone.
Vault: --vault, else the repo's own (DENISSE_VAULT / settings, as atrium.context).
Every name, folder and sentence: scripts/tidy_vault_logs.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import posixpath
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(REPO / "orchestra")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from atrium import conf as aconf  # noqa: E402
from atrium import import_chats as importer  # noqa: E402
from atrium import months  # noqa: E402

CONFIG = HERE / "tidy_vault_logs.json"
WIKI = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
MDLINK = re.compile(r"(\[[^\]\n]*\]\()(<[^>\n]+>|[^)\s]+)((?:\s+\"[^\"\n]*\")?\))")


def cfg() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Plan:
    vault: Path
    moves: list[tuple[str, str]] = field(default_factory=list)   # vault-relative posix
    renames: int = 0
    edits: dict[str, tuple[bytes, bytes]] = field(default_factory=dict)  # old rel -> (before, after)
    links: int = 0
    states: int = 0
    kept: list[tuple[str, str]] = field(default_factory=list)
    folders: dict[str, tuple[int, set]] = field(default_factory=dict)

    def new_of(self) -> dict[str, str]:
        return dict(self.moves)


# ---- where each file goes ----

def _rel(vault: Path, p: Path) -> str:
    return p.relative_to(vault).as_posix()


def _read(p: Path) -> str | None:
    try:
        with p.open(encoding="utf-8-sig", newline="") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _date(p: Path, text: str | None, c: dict) -> str:
    d = months.name_date(p.name)
    if d:
        return d
    if text is not None:
        fm, _ = importer.frontmatter(text.replace("\r\n", "\n"))
        v = importer._scalar(fm, c["date_field"])
        try:
            return dt.date.fromisoformat(v[:10]).isoformat()
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(p.stat().st_mtime).date().isoformat()


def asks(text: str, heading: str) -> list[str]:
    """The bullets under the asks heading, markdown escaping undone."""
    _, body = importer.frontmatter(text.replace("\r\n", "\n"))
    out, inside = [], False
    for line in body:
        if line.startswith("#"):
            inside = line.strip() == heading
        elif inside and line.startswith("- "):
            out.append(re.sub(r"\\(.)", r"\1", line[2:]))
    return out


def preamble_named(items: list[str]) -> bool:
    """True when the note's first ask is a machine preamble, i.e. the importer named it
    after that preamble."""
    skip = [re.compile(p, re.I) for p in aconf.section("import_chats")["title"]["skip"]]
    return bool(items) and any(r.match(items[0].lstrip()) for r in skip)


def _unique(stem: str, ext: str, taken: set[str], cap: int | None) -> str:
    name = stem + ext
    n = 1
    while name.casefold() in taken:
        suffix = f" ({n})"
        base = stem[:cap - len(suffix)].rstrip(" .") if cap else stem
        name = base + suffix + ext
        n += 1
    return name


def plan_moves(vault: Path, root: bool, c: dict) -> Plan:
    plan = Plan(vault)
    cap = aconf.section("import_chats")["title"]["max_chars"]
    for folder in c["folders"]:
        base = vault / folder
        if not base.is_dir():
            continue
        taken = months.names(base)
        flat = sorted(p for p in base.iterdir() if p.is_file())
        n, used = 0, set()
        for p in flat:
            text = _read(p) if p.suffix == ".md" else None
            date = _date(p, text, c)
            month = months.folder(date)
            name = p.name
            if folder in c["rename_in"] and text is not None and preamble_named(asks(text, c["asks_heading"])):
                fm, _ = importer.frontmatter(text.replace("\r\n", "\n"))
                source = importer._scalar(fm, c["source_field"])
                label = importer.ask_title(asks(text, c["asks_heading"]))
                stem = " ".join(x for x in (date, source, label) if x)
                taken.discard(p.name.casefold())
                name = _unique(stem, p.suffix, taken, len(f"{date} {source} ") + cap)
                plan.renames += name != p.name
            else:
                taken.discard(p.name.casefold())
                if (base / month / name).exists():
                    name = _unique(p.stem, p.suffix, taken | {name.casefold()}, None)
            taken.add(name.casefold())
            plan.moves.append((_rel(vault, p), f"{folder}/{month}/{name}"))
            n += 1
            used.add(month)
        if n:
            plan.folders[folder] = (n, used)
    if root:
        r = c["root"]
        dest = vault / r["dest"]
        taken = {p.name.casefold() for p in dest.iterdir()} if dest.is_dir() else set()
        for name in r["move"]:
            p = vault / name
            if p.is_file():
                new = _unique(p.stem, p.suffix, taken, None)
                taken.add(new.casefold())
                plan.moves.append((name, f"{r['dest']}/{new}"))
        plan.kept = [(k, v) for k, v in r["keep"].items() if (vault / k).exists()]
    return plan


# ---- links and state files ----

def _key(path: str) -> str:
    return path.casefold()


def link_maps(plan: Plan) -> tuple[dict, dict]:
    """(old path -> new path, keyed with and without .md; old stem -> new stem for renames)."""
    paths, stems = {}, {}
    for old, new in plan.moves:
        if posixpath.basename(old) != posixpath.basename(new) and "/" not in old:
            continue  # a root file kept apart from its live copy: links should find the live one
        paths[_key(old)] = new
        if old.lower().endswith(".md"):
            paths[_key(old[:-3])] = new[:-3]
        o, n = posixpath.splitext(posixpath.basename(old))[0], posixpath.splitext(posixpath.basename(new))[0]
        if o != n:
            stems[_key(o)] = n
    return paths, stems


def rewrite_wikilinks(text: str, paths: dict, stems: dict) -> tuple[str, int]:
    count = 0

    def sub(m):
        nonlocal count
        inner = m.group(1)
        whole = re.match(r"([^|]*)(.*)", inner, re.S)
        if "/" not in whole.group(1) and _key(whole.group(1).strip()) in stems:
            # an old preamble name with '#' in it, written in full
            count += 1
            return "[[" + stems[_key(whole.group(1).strip())] + whole.group(2) + "]]"
        t = re.match(r"([^#|]*)(.*)", inner, re.S)
        target, rest = t.group(1), t.group(2)
        if target.endswith("\\"):  # [[a\|b]] inside a table
            target, rest = target[:-1], "\\" + rest
        k = _key(target.strip())
        new = None
        if "/" in target and k in paths:
            new = paths[k]
        elif "/" not in target and k in stems:
            new = stems[k]
        if new is None:
            return m.group(0)
        count += 1
        return "[[" + new + rest + "]]"
    return WIKI.sub(sub, text), count


def rewrite_mdlinks(text: str, old_dir: str, new_dir: str, paths: dict, exists) -> tuple[str, int]:
    """Markdown links: relative ones are resolved against the note's old folder, others
    against the vault root; rewritten when the target moved or the note itself did."""
    count = 0

    def sub(m):
        nonlocal count
        raw = m.group(2)
        angle = raw.startswith("<")
        url = raw[1:-1] if angle else raw
        if re.match(r"[a-z][a-z0-9+.-]*:", url, re.I) or url.startswith("#"):
            return m.group(0)
        path, hashpart = (url.split("#", 1) + [""])[:2]
        path = unquote(path)
        if not path:
            return m.group(0)
        if path.startswith("/"):
            mode, old_target = "root", path.lstrip("/")
        else:
            rel_t = posixpath.normpath(posixpath.join(old_dir, path))
            if not rel_t.startswith("..") and (exists(rel_t) or _key(rel_t) in paths):
                mode, old_target = "rel", rel_t
            elif exists(path) or _key(path) in paths:
                mode, old_target = "vault", posixpath.normpath(path)
            else:
                return m.group(0)
        new_target = paths.get(_key(old_target), old_target)
        if new_target == old_target and (mode != "rel" or new_dir == old_dir):
            return m.group(0)  # neither the target nor (for a relative link) the note moved
        if mode == "rel":
            out = posixpath.relpath(new_target, new_dir or ".")
        elif mode == "root":
            out = "/" + new_target
        else:
            out = new_target
        if out == path:
            return m.group(0)
        count += 1
        if angle:
            enc = "<" + out + (("#" + hashpart) if hashpart else "") + ">"
        else:
            enc = quote(out, safe="/~!$&'*+,;=:@-._")  # ( ) encoded: "(1)" would end the link + (("#" + hashpart) if hashpart else "")
        return m.group(1) + enc + m.group(3)
    return MDLINK.sub(sub, text), count


def rename_lines(text: str, old_stem: str, new_stem: str) -> str:
    """A renamed chat note's own name: frontmatter line and H1, as the importer writes them."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    quoted = lambda s: json.dumps(s, ensure_ascii=False).replace("[", r"[").replace("]", r"]")
    in_fm = bool(lines) and lines[0].lstrip("﻿").strip() == "---"
    for i, line in enumerate(lines):
        if in_fm and i and line.strip() == "---":
            in_fm = False
            continue
        if in_fm and line.startswith("name:"):
            v = line[5:].strip()
            try:
                v = json.loads(v) if v.startswith('"') else v
            except ValueError:
                pass
            if v == old_stem:
                lines[i] = "name: " + quoted(new_stem)
        elif not in_fm and line == "# " + importer.markdown(old_stem):
            lines[i] = "# " + importer.markdown(new_stem)
            break
    return nl.join(lines)


def _walk(vault: Path, c: dict):
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = [d for d in dirnames if d not in c["skip_dirs"]]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in c["link_files"]:
                yield Path(dirpath) / fn


def plan_edits(plan: Plan, c: dict) -> None:
    vault = plan.vault
    paths, stems = link_maps(plan)
    new_of = {_key(o): n for o, n in plan.moves}
    full_new = plan.new_of()

    def exists(rel: str) -> bool:
        return (vault / rel).exists()

    for p in _walk(vault, c):
        rel = _rel(vault, p)
        with p.open("rb") as f:
            before = f.read()
        text = _read(p)
        if text is None:
            continue
        new_rel = new_of.get(_key(rel), rel)
        out, n1 = rewrite_wikilinks(text, paths, stems)
        out, n2 = rewrite_mdlinks(out, posixpath.dirname(rel), posixpath.dirname(new_rel), paths, exists)
        if rel in full_new and posixpath.basename(rel) != posixpath.basename(full_new[rel]) and "/" in rel:
            out = rename_lines(out, posixpath.splitext(posixpath.basename(rel))[0],
                               posixpath.splitext(posixpath.basename(full_new[rel]))[0])
        if out != text:
            bom = b"\xef\xbb\xbf" if before.startswith(b"\xef\xbb\xbf") else b""
            plan.edits[rel] = (before, bom + out.encode("utf-8"))
            plan.links += n1 + n2
    for section, key in c["state_files"]:
        rel = aconf.section(section)[key]
        p = vault / rel
        if not p.is_file():
            continue
        before = p.read_bytes()
        try:
            data = json.loads(before.decode("utf-8-sig"))
        except ValueError:
            continue
        changed = [0]

        def fix(v):
            if isinstance(v, dict):
                return {k: fix(x) for k, x in v.items()}
            if isinstance(v, list):
                return [fix(x) for x in v]
            if isinstance(v, str):
                absolute = os.path.isabs(v)
                try:
                    r = Path(v).resolve().relative_to(vault.resolve()).as_posix() if absolute else v.replace("\\", "/")
                except ValueError:
                    return v
                if r in full_new:
                    changed[0] += 1
                    return str(vault / full_new[r]) if absolute else full_new[r]
            return v
        data = fix(data)
        if changed[0]:
            indent = 1 if b'\n "' in before else 2
            after = (json.dumps(data, ensure_ascii=False, indent=indent) + "\n").encode("utf-8")
            plan.edits[rel] = (before, after)
            plan.states += 1


def make_plan(vault: Path, root: bool = False) -> Plan:
    c = cfg()
    plan = plan_moves(Path(vault).resolve(), root, c)
    plan_edits(plan, c)
    return plan


def describe(plan: Plan, dry: bool) -> str:
    c = cfg()
    m = c["messages"]
    lines = [m["dry_header"]] if dry else []
    if not plan.moves and not plan.edits:
        return "\n".join(lines + [m["nothing"]])
    lines.append(m["counts"].format(moves=len(plan.moves), renames=plan.renames, links=plan.links,
                                    files=len(plan.edits) - plan.states, states=plan.states))
    for folder, (n, used) in plan.folders.items():
        lines.append(m["by_folder"].format(folder=folder, n=n, months=len(used)))
    for name, why in plan.kept:
        lines.append(m["kept"].format(name=name, why=why))
    sample = plan.moves[:c["dry_run_sample"]]
    if dry and sample:
        lines.append(m["sample_header"].format(n=len(sample)))
        lines += [m["move_line"].format(old=o, new=n) for o, n in sample]
    return "\n".join(lines)


# ---- doing it, and undoing it ----

def _lock(lock_path):
    import vaultlock
    return vaultlock.VaultLock(lock_path) if lock_path else vaultlock.VaultLock()


def apply(plan: Plan, manifest_dir: Path) -> tuple[Path, list[str]]:
    """Write the manifest and backups first, then move, then write the edits (each only if
    the note is still exactly what was read). Returns (manifest, skipped)."""
    c = cfg()
    vault = plan.vault
    run = manifest_dir / dt.datetime.now().strftime(c["time_format"])
    n = 0
    while run.exists():
        n += 1
        run = manifest_dir / (dt.datetime.now().strftime(c["time_format"]) + f"-{n}")
    (run / "originals").mkdir(parents=True)
    full_new = plan.new_of()
    edits = []
    for i, (rel, (before, after)) in enumerate(sorted(plan.edits.items())):
        backup = f"originals/{i:05d}"
        (run / backup).write_bytes(before)
        edits.append({"path": full_new.get(rel, rel), "was": rel, "backup": backup,
                      "before": _sha(before), "after": _sha(after)})
    created = sorted({posixpath.dirname(n) for _, n in plan.moves if not (vault / posixpath.dirname(n)).exists()})
    manifest = {"vault": str(vault), "created": dt.datetime.now().isoformat(timespec="seconds"),
                "moves": [{"old": o, "new": n} for o, n in plan.moves], "edits": edits,
                "made_dirs": created, "done": False, "skipped": []}
    mpath = run / c["manifest_name"]
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    skipped = []
    for o, n in plan.moves:
        src, dst = vault / o, vault / n
        if dst.exists() or not src.exists():
            skipped.append(f"{o} -> {n}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dst)  # never replaces: fails if dst appeared meanwhile
    by_sha = {e["path"]: e for e in edits}
    for rel, (before, after) in plan.edits.items():
        p = vault / full_new.get(rel, rel)
        if not p.is_file() or _sha(p.read_bytes()) != by_sha[full_new.get(rel, rel)]["before"]:
            skipped.append(full_new.get(rel, rel))
            continue
        tmp = p.with_name(p.name + ".tidy-tmp")
        tmp.write_bytes(after)
        os.replace(tmp, p)
    manifest.update(done=True, skipped=skipped)
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return mpath, skipped


def undo(mpath: Path) -> dict:
    """Restore edited notes still as the tidy left them, move files back, remove the month
    folders the tidy made if they are empty again. Anything changed since is left, and listed."""
    mpath = Path(mpath)
    m = json.loads(mpath.read_text(encoding="utf-8"))
    vault = Path(m["vault"])
    out = {"moves": 0, "edits": 0, "skipped": []}
    for e in m["edits"]:
        p = vault / e["path"]
        if p.is_file() and _sha(p.read_bytes()) == e["after"]:
            tmp = p.with_name(p.name + ".tidy-tmp")
            tmp.write_bytes((mpath.parent / e["backup"]).read_bytes())
            os.replace(tmp, p)
            out["edits"] += 1
        elif not (p.is_file() and _sha(p.read_bytes()) == e["before"]):
            out["skipped"].append(e["path"])
    for mv in reversed(m["moves"]):
        src, dst = vault / mv["new"], vault / mv["old"]
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)
            out["moves"] += 1
        elif not dst.exists():
            out["skipped"].append(mv["new"])
    for d in sorted(m.get("made_dirs", []), key=len, reverse=True):
        try:
            (vault / d).rmdir()  # only ever succeeds on an empty folder
        except OSError:
            pass
    return out


def run(vault: Path, *, dry: bool, root: bool, manifest_dir: Path, lock_path: Path | None = None,
        say=print) -> Path | None:
    c = cfg()
    if dry:
        say(describe(make_plan(vault, root), dry=True))
        return None
    try:
        with _lock(lock_path):
            plan = make_plan(vault, root)
            if not plan.moves and not plan.edits:
                say(describe(plan, dry=False))
                return None
            mpath, skipped = apply(plan, manifest_dir)
    except TimeoutError:
        say(c["messages"]["locked"])
        return None
    say(describe(plan, dry=False))
    for s in skipped:
        say(c["messages"]["skipped"].format(what=s))
    say(c["messages"]["done"].format(moves=len(plan.moves) - sum("->" in s for s in skipped),
                                     files=len(plan.edits) - plan.states, states=plan.states, manifest=mpath))
    return mpath


def main(argv=None) -> int:
    c = cfg()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", type=Path, help="the vault (default: the repo's configured one)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    ap.add_argument("--root", action="store_true", help="also move the configured root files")
    ap.add_argument("--undo", type=Path, metavar="MANIFEST", help="undo a run from its manifest")
    ap.add_argument("--manifest-dir", type=Path, default=REPO / c["manifest_dir"])
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if a.undo:
        with _lock(None):
            out = undo(a.undo)
        print(c["messages"]["undo_done"].format(moves=out["moves"], edits=out["edits"], skipped=len(out["skipped"])))
        for s in out["skipped"]:
            print(c["messages"]["skipped"].format(what=s))
        return 0
    if a.vault is None:
        from atrium.context import VAULT
        a.vault = VAULT
    run(a.vault, dry=a.dry_run, root=a.root, manifest_dir=a.manifest_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
