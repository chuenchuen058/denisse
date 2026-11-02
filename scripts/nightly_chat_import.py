"""Nightly step: import new Claude Code / Codex sessions into the vault.

Called from nightly.py as its own stage. Everything here is bounded:

  * sessions modified in the last SETTLE seconds are skipped -- they are
    probably still running, and importing half a session would record its ID
    as done. They are picked up whole on a later night.
  * at most CAP notes per night across all sources; the importer leaves the
    rest unrecorded, so the backlog drains over the following nights.
  * a claude.ai export dropped in ~/Downloads (claude-export*.zip or
    conversations.json) is imported from a temporary copy; its SHA-256 goes
    into the import state so the same file is never imported twice. The
    download itself is only ever read -- never moved or deleted.
  * the commit stages ONLY the notes written tonight plus the state file,
    by literal path, via vault_git.commit_owned.

Any failure is reported through `say` and swallowed: this step must never
stop the rest of the nightly.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (HERE, REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vault_git import BackupError, commit_owned  # noqa: E402

CAP = 200
SETTLE = 30 * 60
SESSION_SOURCES = (("claude-code", (".claude", "projects")),
                   ("codex", (".codex", "sessions")))
EXPORT_KEY = "claude-export-file:"
MESSAGE = "Nightly chat import"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def find_exports(downloads: Path, settled_before: float) -> list[Path]:
    """Top-level export files in Downloads, oldest first, finished downloading."""
    if not downloads.is_dir():
        return []
    found = [p for p in downloads.iterdir() if p.is_file() and (
        p.name.lower() == "conversations.json"
        or (p.name.lower().startswith("claude-export") and p.suffix.lower() == ".zip"))]
    return sorted((p for p in found if p.stat().st_mtime < settled_before),
                  key=lambda p: (p.stat().st_mtime, p.name))


def stage_export(export: Path, tmp: Path) -> Path:
    """Copy the export's conversations.json into tmp; return the copy.

    Only the one member is extracted, written under a fixed name, so a
    hostile archive path cannot land anywhere else (no zip-slip)."""
    target = tmp / "conversations.json"
    if export.suffix.lower() == ".zip":
        with zipfile.ZipFile(export) as z:
            members = [m for m in z.infolist() if not m.is_dir()
                       and m.filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
                       == "conversations.json"]
            if not members:
                raise ValueError("export zip has no conversations.json")
            # The shallowest one is the export's own; deeper ones are unexpected.
            member = min(members, key=lambda m: m.filename.count("/"))
            with z.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        shutil.copyfile(export, target)
    return target


def read_state(vault: Path, importer) -> dict:
    path = vault / importer.STATE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def record_export(vault: Path, importer, digest: str, name: str) -> None:
    path = importer.checked(vault, importer.STATE)
    state = read_state(vault, importer)
    state[EXPORT_KEY + digest] = f"{time.strftime('%Y-%m-%d')} {name}"
    importer.checked(vault, importer.STATE.parent).mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(vault, *, home=None, downloads=None, now=None, cap=CAP, apply=True,
        commit=True, say=print) -> dict:
    """Run the import step. Returns counts; never raises."""
    counts = {"imported": 0, "exports": 0, "failed": 0, "commit": "none"}
    try:
        from atrium import import_chats as importer
        vault = Path(vault).resolve()
        home = Path(home) if home else Path.home()
        downloads = Path(downloads) if downloads else home / "Downloads"
        settled = (time.time() if now is None else now) - SETTLE
    except Exception as exc:  # an import error must not stop the nightly
        say(f"  chat import unavailable: {type(exc).__name__}")
        counts["failed"] += 1
        return counts

    written: list[Path] = []
    state_changed = False

    def take(source, path, label):
        nonlocal state_changed
        remaining = cap - len(written)
        if remaining <= 0:
            say(f"  {label}: skipped, nightly cap of {cap} reached")
            return None
        # The importer prints one line per note; keep the nightly log to counts.
        with contextlib.redirect_stdout(io.StringIO()):
            notes = importer.import_chats(source, path, vault, apply=apply,
                                          limit=remaining, settled_before=settled)
        written.extend(notes)
        state_changed = state_changed or bool(notes)
        say(f"  {label}: {len(notes)} {'imported' if apply else 'would import'}")
        return notes, remaining

    for source, parts in SESSION_SOURCES:
        path = home.joinpath(*parts)
        if not path.is_dir():
            say(f"  {source}: no session directory")
            continue
        try:
            take(source, path, source)
        except Exception as exc:
            # Exception text may quote private chat content; the type is enough.
            counts["failed"] += 1
            say(f"  {source}: import failed ({type(exc).__name__}); other sources continue")

    try:
        exports = find_exports(downloads, settled)
    except OSError as exc:
        exports = []
        counts["failed"] += 1
        say(f"  downloads: could not scan ({type(exc).__name__})")
    for export in exports:
        try:
            digest = sha256(export)
            if EXPORT_KEY + digest in read_state(vault, importer):
                continue
            with tempfile.TemporaryDirectory() as tmp:
                staged = stage_export(export, Path(tmp))
                result = take("claude-export", staged, f"export {export.name}")
            if result is None:
                continue
            notes, remaining = result
            if len(notes) >= remaining:
                # Capped mid-file: leave the hash unrecorded so tomorrow finishes it.
                say(f"  export {export.name}: cap reached, rest tomorrow")
                continue
            if apply:
                record_export(vault, importer, digest, export.name)
                state_changed = True
            counts["exports"] += 1
        except Exception as exc:
            counts["failed"] += 1
            say(f"  export {export.name}: import failed ({type(exc).__name__})")

    counts["imported"] = len(written)
    if not written and not state_changed:
        say("  no new chats")
    if not (apply and commit and state_changed):
        return counts
    paths = [p.relative_to(vault).as_posix() for p in written]
    paths.append(importer.STATE.as_posix())
    try:
        sha = commit_owned(str(vault), paths, MESSAGE)
        counts["commit"] = sha[:7] if sha else "none"
        say(f"  committed {sha[:7]}" if sha else "  nothing to commit")
    except BackupError:
        counts["failed"] += 1
        counts["commit"] = "failed"
        say("  chat import commit failed; notes are on disk, uncommitted")
    return counts
