"""Rebuild the Calendar/Topics hubs: one note per topic linking every chat and session log.

Chuen, 15 Nov 2026: graph view was a cloud of orphan logs, so the logs were grouped by hand into
topic hubs. New logs arrive every night (nightly_chat_import.py, atrium.capture), so nightly.py
runs this after the chat import and the hubs keep up by themselves.

Everything that names a topic, a pattern, an anchor note, a heading or a limit is in
topic_hubs.json (Rule 0). This file only knows the shape:

  * a log is a .md file under one of the "sources" folders; its date is the leading date in its
    name (or the frontmatter date), its text for matching is its name plus its asked section.
  * it goes to the first topic whose patterns match, else the fallback topic.
  * a topic over "split_over" logs becomes a hub of ISO-week sub-hubs.
  * links are explicit vault paths WITH .md, so names that contain dots resolve; aliases have the
    characters that break a wikilink (brackets, backticks, pipes) dropped.
  * a hub file is written only when its content changed (the 'updated:' date alone doesn't count),
    under the vault lock (orchestra/vaultlock.py), never over a newer save.

    .venv\\Scripts\\python.exe scripts\\topic_hubs.py           # what would change
    .venv\\Scripts\\python.exe scripts\\topic_hubs.py --apply   # write it
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CONFIG = HERE / "topic_hubs.json"
for _p in (str(HERE), str(REPO / "orchestra")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vaultlock  # noqa: E402  the one lock for vault writes; also write_atomic / replace_if_unchanged


def load_config(path: Path | None = None) -> dict:
    return json.loads(Path(path or CONFIG).read_text(encoding="utf-8"))


# --------------------------------------------------------------- reading ----
def _frontmatter(text: str) -> tuple[dict, str]:
    """Flat key: value pairs and the body after the --- block (the vault's own flat style)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm = {}
    for line in text[3:end].splitlines():
        k, sep, v = line.partition(":")
        if sep and not line.startswith((" ", "#")):
            fm[k.strip()] = v.strip().strip('"')
    return fm, text[end + 4:].split("\n", 1)[-1]


def _section(body: str, headings: list[str]) -> str:
    """The text under any heading named in `headings`, up to the next heading of any level."""
    out, inside = [], False
    for line in body.splitlines():
        if line.startswith("#"):
            inside = line.lstrip("#").strip() in headings
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def read_logs(vault: Path, cfg: dict) -> tuple[list[dict], list[str]]:
    """Every log under the source folders: {rel, stem, date, text}. Also the skipped rel paths
    (no date, or a name a wikilink can't hold)."""
    date_re = re.compile(cfg["date_pattern"])
    unesc = re.compile(cfg["unescape_pattern"])
    bad = set(cfg["target_forbidden_chars"])
    logs, skipped = [], []
    for src in cfg["sources"]:
        root = vault / src
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in cfg["skip_dirs"])
            for fn in sorted(filenames):
                if not fn.endswith(".md"):
                    continue
                path = Path(dirpath) / fn
                rel = path.relative_to(vault).as_posix()
                stem = fn[:-3]
                try:
                    text = path.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    skipped.append(rel)
                    continue
                fm, body = _frontmatter(text)
                m = date_re.match(stem) or date_re.match(fm.get(cfg["frontmatter_date_key"], ""))
                try:
                    day = dt.date.fromisoformat(m.group(1)) if m else None
                except ValueError:
                    day = None
                if day is None or bad & set(rel):
                    skipped.append(rel)
                    continue
                asked = unesc.sub(r"\1", _section(body, cfg["asked_headings"]))
                logs.append({"rel": rel, "stem": stem, "date": day, "title": stem,
                             "text": stem + "\n" + asked})
    return logs, skipped


def note_names(vault: Path, cfg: dict) -> set[str]:
    """Basenames (without .md) of every note in the vault: what a bare [[anchor]] can resolve to."""
    names = set()
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = [d for d in dirnames if d not in cfg["skip_dirs"]]
        names.update(fn[:-3] for fn in filenames if fn.endswith(".md"))
    return names


# ---------------------------------------------------------- classifying ----
def _compiled(topic: dict) -> tuple[list, list]:
    flags = re.IGNORECASE
    return ([re.compile(p, flags) for p in topic.get("title", [])],
            [re.compile(p, flags) for p in topic.get("asked", [])])


def classify(logs: list[dict], cfg: dict) -> dict[str, list[dict]]:
    """topic name -> its logs, newest first. First matching topic wins; the fallback takes the rest."""
    rules = [(t["name"], *_compiled(t)) for t in cfg["topics"] if not t.get("fallback")]
    fallback = next(t["name"] for t in cfg["topics"] if t.get("fallback"))
    out: dict[str, list[dict]] = {t["name"]: [] for t in cfg["topics"]}
    for log in logs:
        name = next((n for n, title, asked in rules
                     if any(p.search(log["title"]) for p in title)
                     or any(p.search(log["text"]) for p in asked)), fallback)
        out[name].append(log)
    for items in out.values():
        items.sort(key=lambda g: (g["date"], g["stem"]), reverse=True)
    return out


def week_of(day: dt.date, cfg: dict) -> str:
    year, week, _ = day.isocalendar()
    return cfg["week_format"].format(year=year, week=week)


# ------------------------------------------------------------- rendering ----
def alias(stem: str, cfg: dict) -> str:
    drop = set(cfg["alias_drop_chars"])
    return "".join(c for c in stem if c not in drop)[:cfg["alias_chars"]].rstrip()


def _link(log: dict, cfg: dict) -> str:
    return f"- [[{log['rel']}|{alias(log['stem'], cfg)}]]"


def _note(name: str, description: str, updated: str, body: list[str], cfg: dict) -> str:
    desc = description.replace('"', "'")
    head = ["---", f"name: {name}", f'description: "{desc}"', f"type: {cfg['type']}",
            f"tags: [{', '.join(cfg['tags'])}]", f"updated: {updated}", "---", f"# {name}", ""]
    return "\n".join(head + body) + "\n"


def _related(names: list[str], cfg: dict) -> str:
    return f"{cfg['related_label']} " + cfg["separator"].join(f"[[{n}]]" for n in names)


def build(vault: Path, cfg: dict) -> tuple[dict[str, tuple], dict]:
    """{hub file name: (note name, description, body lines)} and a summary. Pure: writes nothing."""
    logs, skipped = read_logs(vault, cfg)
    groups = classify(logs, cfg)
    known = note_names(vault, cfg)
    index = cfg["index"]["name"]
    hubs: dict[str, tuple] = {}
    listed = []
    for topic in cfg["topics"]:
        name, items = topic["name"], groups[topic["name"]]
        if not items:
            continue
        listed.append(name)
        anchors = [a for a in topic.get("anchors", []) if a in known] + [index]
        prefix = topic.get("week_prefix", name)
        if len(items) <= cfg["split_over"]:
            desc = cfg["describe"]["flat"].format(name=name, lower=name.lower(), prefix=prefix,
                                                  count=len(items))
            body = [_related(anchors, cfg), "", "", f"## {cfg['logs_heading']}", ""]
            hubs[name] = (name, desc, body + [_link(g, cfg) for g in items])
            continue
        weeks: dict[str, list[dict]] = {}
        for g in items:
            weeks.setdefault(week_of(g["date"], cfg), []).append(g)
        week_names = []
        for wk in sorted(weeks, reverse=True):
            wname = f"{prefix} {wk}"
            week_names.append(wname)
            desc = cfg["describe"]["week"].format(prefix=prefix, week=wk, count=len(weeks[wk]))
            body = [_related([name], cfg), "", "", f"## {cfg['logs_heading']}", ""]
            hubs[wname] = (wname, desc, body + [_link(g, cfg) for g in weeks[wk]])
        desc = cfg["describe"]["split"].format(name=name, lower=name.lower(), prefix=prefix,
                                               count=len(items))
        body = [_related(anchors, cfg), "", "", f"## {cfg['weeks_heading']}", ""]
        hubs[name] = (name, desc, body + [f"- [[{w}]]" for w in week_names])
    back = [b for b in cfg["index"]["back_to"] if b in known]
    body = ([f"{cfg['index']['back_label']} " + cfg["separator"].join(f"[[{b}]]" for b in back), ""]
            if back else [])
    hubs[index] = (index, cfg["index"]["description"],
                   body + [f"- [[{n}]]" for n in sorted(listed, key=str.lower)])
    summary = {"logs": len(logs), "skipped": skipped,
               "topics": {n: len(groups[n]) for n in listed}}
    return hubs, summary


def _updated_of(text: str) -> str | None:
    return _frontmatter(text)[0].get("updated") or None


# --------------------------------------------------------------- writing ----
def run(vault, *, cfg: dict | None = None, today: dt.date | None = None, apply: bool = True,
        commit: bool = False, lock=None, say=print) -> dict:
    """Rebuild the hubs. Returns {"changed": [rel paths], "logs": n, "skipped": [...], "commit": ...}.
    `lock` is a context manager (default: the vault lock); the writes and the commit go inside it."""
    vault = Path(vault)
    cfg = cfg or load_config()
    stamp = (today or dt.date.today()).isoformat()
    hubs, summary = build(vault, cfg)
    folder = vault / cfg["folder"]
    changed: list[str] = []
    result = {"changed": changed, "logs": summary["logs"], "skipped": summary["skipped"],
              "topics": summary["topics"], "commit": "none"}

    def render_for(name, desc, body):
        def render(old: str) -> str:
            # Same content under the old date is no change: keep the file as it is.
            kept = _updated_of(old)
            if kept and _note(name, desc, kept, body, cfg) == old:
                return old
            return _note(name, desc, stamp, body, cfg)
        return render

    guard = (lock if lock is not None else vaultlock.VaultLock()) if apply else contextlib.nullcontext()
    with guard:
        if apply:
            folder.mkdir(parents=True, exist_ok=True)
        for fname, (name, desc, body) in hubs.items():
            path = folder / f"{fname}.md"
            rel = path.relative_to(vault).as_posix()
            render = render_for(name, desc, body)
            if apply:
                if vaultlock.replace_if_unchanged(path, render):
                    changed.append(rel)
            else:
                old = path.read_text(encoding="utf-8") if path.exists() else ""
                if render(old) != old or not path.exists():
                    changed.append(rel)
        if apply and commit and changed:
            from vault_git import BackupError, commit_owned
            try:
                sha = commit_owned(str(vault), changed, cfg["commit_message"])
                result["commit"] = sha[:7] if sha else "none"
            except BackupError:
                result["commit"] = "failed"
    say(f"  {summary['logs']} logs in {len(summary['topics'])} topics; "
        f"{len(changed)} hub(s) {'rewritten' if apply else 'would change'}"
        + (f"; {len(summary['skipped'])} skipped (no date or unlinkable name)" if summary["skipped"] else "")
        + (f"; commit {result['commit']}" if result["commit"] != "none" else ""))
    return result


def nightly_step(vault, say, *, apply: bool = True, commit: bool = True) -> dict:
    """nightly.py's stage: never raises, so a failure here doesn't stop the rest of the night."""
    say("== topics ==")
    try:
        return run(vault, apply=apply, commit=commit, say=say)
    except Exception as exc:  # noqa: BLE001 -- logged, never fatal
        say(f"  topic hubs failed: {type(exc).__name__}: {exc}")
        return {"changed": [], "logs": 0, "skipped": [], "topics": {}, "commit": "failed"}


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    import settings
    out = run(settings.vault(), apply="--apply" in sys.argv)
    for rel in out["changed"]:
        print("   ", rel)
