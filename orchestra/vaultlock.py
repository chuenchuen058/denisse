"""One lock for every Orchestra write into Chuen's vault, and a write that never lands over a
newer edit (audit 10 Nov 2026, O6: up to eight ticks finish within seconds of each other; two
that read the same day note dropped one section, and a tick that read hot.md just before Chuen
saved it in Obsidian wrote the old text back over his edit).

  * VaultLock: a lock file under the repo's .review/ (O_EXCL create, owner pid inside). A lock
    whose owner process is gone is taken over at once; any lock older than the stale time is
    taken over too. The same idea as runner._FileLock, copied in its minimum form so that
    aftercare.py and state_now.py don't need runner.py imported first.
  * replace_if_unchanged(): read a note, build the new text, and write it (temp file in the same
    folder + os.replace) only if the note is still exactly what was read.

Name, wait, stale and poll times: config.json vault_lock_* (mirrored in config_defaults.json).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("ORCHESTRA_REPO", HERE.parent))
KEYS = ("vault_lock_name", "vault_lock_wait_s", "vault_lock_stale_s", "vault_lock_poll_s")


def settings() -> dict:
    """The vault_lock_* keys: config.json, falling back to config_defaults.json key by key."""
    out = {}
    for name in ("config_defaults.json", "config.json"):  # later wins
        try:
            data = json.loads((HERE / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.update({k: data[k] for k in KEYS if k in data})
    return out


def lock_dir() -> Path:
    return REPO / ".review"


def pid_alive(pid) -> bool:
    """True if a process with this pid is running (or exists and isn't ours to query)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True  # another thread of this process holds it
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            return not (k.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259)  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _retry(fn, tries: int = 40, pause: float = 0.05):
    """fn(), retried while Windows says the file is in use (a reader holds it open)."""
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(pause)


class VaultLock:
    """with VaultLock(): ... -- every vault write and the vault repo's git add/commit go inside.
    Raises TimeoutError after the wait time if a live holder keeps it."""

    def __init__(self, path: Path | None = None, wait: float | None = None,
                 stale: float | None = None, poll: float | None = None):
        cfg = settings()
        self.path = Path(path) if path else lock_dir() / cfg["vault_lock_name"]
        self.wait = cfg["vault_lock_wait_s"] if wait is None else wait
        self.stale = cfg["vault_lock_stale_s"] if stale is None else stale
        self.poll = cfg["vault_lock_poll_s"] if poll is None else poll
        self.mine = False

    def _holder(self) -> dict | None:
        for i in range(5):  # being written by its owner for an instant: re-read a few times
            try:
                info = json.loads(self.path.read_text(encoding="utf-8"))
                return info if isinstance(info, dict) else None
            except (OSError, ValueError):
                if i < 4:
                    time.sleep(0.02)
        return None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        end = time.time() + self.wait
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"pid": os.getpid(), "since": time.time()}, f)
                self.mine = True
                return self
            except FileExistsError:
                pass
            except PermissionError:  # the old lock is mid-delete on Windows
                time.sleep(self.poll)
                continue
            info = self._holder()
            try:
                age = time.time() - self.path.stat().st_mtime
            except FileNotFoundError:
                continue
            dead = bool(info and info.get("pid")) and not pid_alive(info["pid"])
            if dead or age > self.stale:
                self._remove()
                continue
            if time.time() > end:
                raise TimeoutError(f"{self.path.name} held too long by pid {(info or {}).get('pid', '?')}")
            time.sleep(self.poll)

    def _remove(self) -> None:
        try:
            _retry(lambda: self.path.unlink(missing_ok=True))
        except PermissionError:
            pass

    def __exit__(self, *exc):
        if self.mine:
            self._remove()
            self.mine = False


def _snapshot(path: Path) -> tuple | None:
    try:
        st = path.stat()
        return st.st_size, st.st_mtime_ns, _retry(path.read_bytes)
    except FileNotFoundError:
        return None


def write_atomic(path: Path, data: bytes) -> None:
    """data into path via a temp file in the same folder and os.replace."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        _retry(lambda: os.replace(tmp, path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def replace_if_unchanged(path: Path, render) -> bool:
    """Call under VaultLock. Reads path (bytes; '' if missing), new = render(old_text), and
    writes new only if it differs AND the file is still byte-for-byte what was read (size, mtime
    and content). Someone else's newer save (Chuen in Obsidian) is left alone: False, and the
    next tick tries again. True when the file was written."""
    before = _snapshot(path)
    old = before[2].decode("utf-8") if before else ""
    new = render(old)
    if new == old and before is not None:
        return False
    if _snapshot(path) != before:
        return False
    write_atomic(path, new.encode("utf-8"))
    return True
