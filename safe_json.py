"""Read, write and lock Denisse's own JSON data files without losing them.

The pattern it replaces, in memory_store, tasks, tools (facts), optimize and
others: a read that failed to parse returned "empty", and the next save wrote
that empty list over the file in place with open(path, "w"). One torn write
(a crash mid-save), or a second process reading while the first was half-way
through writing, and the whole file was gone.

- ``read_json(path, default)``: a missing file is ``default``. A file that
  exists but won't parse is copied aside to ``<stem>.corrupt-<UTC><ext>``
  first (one log line), then ``default`` -- so the caller's next save can go
  ahead and the damaged file is still there to recover from. A file that
  can't be opened (still locked by a replace) is retried, then raises: it is
  never read as empty.
- ``write_json_atomic(path, data, **dump_kwargs)``: a temp file in the same
  folder, flushed and fsynced, then ``os.replace``d over the target (retried
  on Windows' PermissionError). A save killed at any point leaves either the
  old file or the new one, whole.
- ``with locked(path):`` around a read-modify-write: an OS lock on a lock
  file next to the data file, so two processes (the tray, scripts/ask.py,
  the eval, the agent loop) take turns. The OS drops the lock when its holder
  dies, so a lock file left behind by a dead process never blocks. Re-entrant
  within one thread.

Every number and name lives in safe_json.json (AGENTS.md Rule 0).
"""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import json_config

log = logging.getLogger("denisse.safe_json")

_HERE = os.path.dirname(os.path.abspath(__file__))
_NAME = os.path.splitext(os.path.basename(__file__))[0]
CONFIG_FILE = os.path.join(_HERE, _NAME + ".json")
DEFAULTS_FILE = os.path.join(_HERE, _NAME + ".defaults.json")

CONFIG: dict | None = None


def configure(path=None) -> dict:
    """(Re)load safe_json.json, or ``path``."""
    global CONFIG
    CONFIG = json_config.load(_NAME, path or CONFIG_FILE, DEFAULTS_FILE)
    return CONFIG


def _cfg(key):
    if CONFIG is None:
        configure()
    return CONFIG[key]


# --- reading ---------------------------------------------------------------

def corrupt_path(path, when: datetime | None = None) -> str:
    """Where a damaged ``path`` is copied: next to it, named by
    safe_json.json's corrupt_name."""
    folder, name = os.path.split(os.path.abspath(path))
    stem, ext = os.path.splitext(name)
    stamp = (when or datetime.now(timezone.utc)).strftime(_cfg("corrupt_stamp_format"))
    return os.path.join(folder, _cfg("corrupt_name").format(stem=stem, ext=ext, stamp=stamp))


def corrupt_copies(path) -> list:
    """The copies set aside for ``path`` so far, oldest name first."""
    folder, name = os.path.split(os.path.abspath(path))
    stem, ext = os.path.splitext(name)
    pattern = _cfg("corrupt_name").format(stem=glob.escape(stem), ext=glob.escape(ext), stamp="*")
    return sorted(glob.glob(os.path.join(folder, pattern)))


def _set_aside(path, raw: bytes, why) -> None:
    # The same damaged bytes are kept once: every read of a still-damaged
    # file (replay() runs each turn) must not pile up another copy.
    for existing in corrupt_copies(path):
        try:
            with open(existing, "rb") as f:
                if f.read() == raw:
                    log.warning("%s won't parse (%s); already kept as %s -- using the default",
                                path, why, existing)
                    return
        except OSError:
            continue
    dest = corrupt_path(path)
    base, ext = os.path.splitext(dest)
    n = 1
    while os.path.exists(dest):  # two in the same second
        dest = f"{base}-{n}{ext}"
        n += 1
    with open(dest, "wb") as f:
        f.write(raw)
    log.warning("%s won't parse (%s); kept as %s -- using the default", path, why, dest)


def _retry(fn, attempts_key, wait_key):
    attempts = _cfg(attempts_key)
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(_cfg(wait_key))


def read_json(path, default):
    """The parsed contents of ``path``; ``default`` if it doesn't exist or
    is damaged (a damaged one is copied aside first). Raises OSError only
    if the file exists and stays unopenable."""
    def _raw():
        with open(path, "rb") as f:
            return f.read()

    try:
        raw = _retry(_raw, "read_attempts", "read_retry_seconds")
    except FileNotFoundError:
        return default
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError both
        _set_aside(path, raw, exc)
        return default


# --- writing ---------------------------------------------------------------

def write_json_atomic(path, data, **dump_kwargs) -> None:
    """Write ``data`` to ``path`` so that a crash at any moment leaves the
    old file or the new one, never half of either. ``dump_kwargs`` go to
    json.dump (indent, ensure_ascii...)."""
    path = os.path.abspath(path)
    folder, name = os.path.split(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=_cfg("temp_prefix").format(name=name),
                               suffix=_cfg("temp_suffix"))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, **dump_kwargs)
            f.flush()
            os.fsync(f.fileno())
        _retry(lambda: os.replace(tmp, path), "replace_attempts", "replace_retry_seconds")
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# --- locking ---------------------------------------------------------------

_held = threading.local()


def _try_lock(fh) -> bool:
    fh.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh) -> None:
    fh.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_UN)


def lock_path(path) -> str:
    return os.path.abspath(path) + _cfg("lock_suffix")


@contextmanager
def locked(path):
    """Hold ``path``'s cross-process lock for the body of the ``with``.

    Re-entrant in one thread. A holder that is alive but stuck is waited on
    for lock_wait_seconds; then the body runs anyway with one log line --
    the write is still atomic, and the tray must keep working.
    """
    key = os.path.normcase(os.path.abspath(path))
    depth = getattr(_held, "depth", None)
    if depth is None:
        depth = _held.depth = {}
    if depth.get(key):
        depth[key] += 1
        try:
            yield
        finally:
            depth[key] -= 1
        return

    lp = lock_path(path)
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    fh = open(lp, "a+b")
    got = False
    try:
        deadline = time.monotonic() + _cfg("lock_wait_seconds")
        while not (got := _try_lock(fh)):
            if time.monotonic() >= deadline:
                log.warning("%s still locked after %ss -- going ahead without it",
                            lp, _cfg("lock_wait_seconds"))
                break
            time.sleep(_cfg("lock_poll_seconds"))
        depth[key] = 1
        try:
            yield
        finally:
            depth.pop(key, None)
            if got:
                _unlock(fh)
    finally:
        fh.close()
