"""One local inference at a time: a file lease at <main checkout>/.review/gpu.lease.

Orchestra's local team takes it for each model call and skips (never waits) when it is
held. Denisse's own heavy GPU jobs (eval/run_eval.py) take it too, waiting a while for a
local-team call to finish. The lease lives in the *main* checkout's .review even when the
caller runs in a worktree, so every worktree sees the same one.

A lease whose holder process is gone, or that is older than its ttl, is stale and taken
over. Nothing here kills, unloads or pulls anything.

    from orchestra.local_team.lease import GpuLease
    with GpuLease.for_repo(repo, "eval/run_eval.py").hold(wait_s=300) as got:
        ...  # got is False if it was still busy after wait_s
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_TTL_S = 900


def main_checkout(repo: Path) -> Path:
    """The main checkout for a worktree (its .git is a file pointing into <main>/.git/worktrees)."""
    git = Path(repo) / ".git"
    try:
        if git.is_file():
            line = git.read_text(encoding="utf-8").strip()
            if line.startswith("gitdir:"):
                gitdir = Path(line[len("gitdir:"):].strip())
                if not gitdir.is_absolute():
                    gitdir = (Path(repo) / gitdir).resolve()
                if gitdir.parent.name == "worktrees":
                    return gitdir.parent.parent.parent
    except OSError:
        pass
    return Path(repo)


def pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.GetLastError() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)  # POSIX only: on Windows os.kill terminates the process
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class GpuLease:
    def __init__(self, path: Path, holder: str, ttl_s: int = DEFAULT_TTL_S):
        self.path, self.holder, self.ttl_s = Path(path), holder, ttl_s
        self.held = False

    @classmethod
    def for_repo(cls, repo: Path, holder: str, ttl_s: int = DEFAULT_TTL_S) -> "GpuLease":
        override = os.environ.get("ORCHESTRA_GPU_LEASE")
        path = Path(override) if override else main_checkout(repo) / ".review" / "gpu.lease"
        return cls(path, holder, ttl_s)

    def info(self) -> dict | None:
        """Who holds it, or None if free or stale."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            data = {}
        return None if self._stale(data) else data

    def _stale(self, data: dict) -> bool:
        if data.get("pid") is not None and not pid_alive(data.get("pid")):
            return True
        try:
            since = dt.datetime.fromisoformat(data["since"])
            ttl = int(data.get("ttl_s", self.ttl_s))
        except (KeyError, TypeError, ValueError):
            try:  # unreadable: judge by file age alone
                return time.time() - self.path.stat().st_mtime > self.ttl_s
            except OSError:
                return True
        return (dt.datetime.now() - since).total_seconds() > ttl

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self.info() is not None:
                    return False
                with contextlib.suppress(FileNotFoundError):
                    self.path.unlink()  # stale: take it over
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "holder": self.holder, "ttl_s": self.ttl_s,
                           "since": dt.datetime.now().replace(microsecond=0).isoformat()}, f)
            self.held = True
            return True
        return False

    def acquire(self, wait_s: float = 0, poll_s: float = 2) -> bool:
        deadline = time.monotonic() + wait_s
        while True:
            if self.try_acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_s)

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("pid") == os.getpid():  # never delete someone else's lease
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()

    @contextlib.contextmanager
    def hold(self, wait_s: float = 0, poll_s: float = 2):
        got = self.acquire(wait_s, poll_s)
        try:
            yield got
        finally:
            if got:
                self.release()
