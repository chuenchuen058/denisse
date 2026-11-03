"""What the running tray is doing, readable from outside it.

Timers and study mode live only in the tray's memory, so before
2026-10-30 nobody could tell whether restarting it would cut off a
countdown (two sessions declined to restart it for exactly that reason),
and importing timers.py in another process only shows that process's own
empty state. The tray now writes a small snapshot every few seconds:

    memory/runtime_status.json   (private: memory/*.json is gitignored)

    .venv\\Scripts\\python.exe runtime_status.py   -> the snapshot, whether
    that process is still alive, and whether a restart would interrupt
    anything.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time

import runtime_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(BASE_DIR, "memory", "runtime_status.json")


def _cfg():
    """Intervals, limits and wording: runtime.defaults.json, overridden by
    config.json settings.runtime.runtime_status (runtime_config)."""
    return runtime_config.get("runtime_status")


def _say(key, **fields):
    return runtime_config.wording("runtime_status", key, **fields)


def every_seconds():
    return _cfg()["write_every_seconds"]


def stale_seconds():
    return _cfg()["stale_after_writes"] * every_seconds()


def code_version(root=BASE_DIR):
    """The commit checked out when this was called -- read from .git, no
    subprocess. The tray calls it once at startup: what it loaded."""
    try:
        git = os.path.join(root, ".git")
        if os.path.isfile(git):  # a linked worktree: ".git" is "gitdir: <path>"
            with open(git, encoding="utf-8") as f:
                line = f.read().strip()
            if not line.startswith("gitdir: "):
                return "unknown"
            git = os.path.normpath(os.path.join(root, line[8:]))
        common = git  # branch refs live in the main repo's .git for worktrees
        try:
            with open(os.path.join(git, "commondir"), encoding="utf-8") as f:
                common = os.path.normpath(os.path.join(git, f.read().strip()))
        except FileNotFoundError:
            pass
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if not head.startswith("ref: "):
            return head[:12]
        ref = head[5:]
        try:
            with open(os.path.join(common, *ref.split("/")), encoding="utf-8") as f:
                return f.read().strip()[:12]
        except FileNotFoundError:
            with open(os.path.join(common, "packed-refs"), encoding="utf-8") as f:
                for line in f:
                    if line.rstrip().endswith(" " + ref):
                        return line.split()[0][:12]
    except OSError:
        pass
    return "unknown"


def _write(snapshot, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".runtime-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    os.replace(tmp, path)


def start(collect, path=None, every=None):
    """Write {pid, started, code, written, **collect()} every `every`
    seconds on a daemon thread. A failing collect is recorded, not raised:
    the tray must never stop because its status report did."""
    path = path or STATUS_FILE
    every = every or every_seconds()
    fixed = {"pid": os.getpid(), "started": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
             "code": code_version()}

    def once():
        try:
            state = collect()
        except Exception as exc:  # noqa: BLE001
            state = {"collect_error": f"{type(exc).__name__}: {exc}"[:_cfg()["collect_error_chars"]]}
        try:
            _write({**fixed, "written": dt.datetime.now().astimezone().isoformat(timespec="seconds"), **state}, path)
        except OSError:
            pass

    def loop():
        while True:
            once()
            time.sleep(every)

    threading.Thread(target=loop, daemon=True, name="runtime-status").start()
    return once


def _alive(pid):
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def report(path=None, now=None):
    """The snapshot plus a verdict: (dict, list of reasons a restart would
    interrupt something, or why the snapshot can't be trusted)."""
    path = path or STATUS_FILE
    attempts = _cfg()["read_attempts"]
    for attempt in range(attempts):
        try:
            with open(path, encoding="utf-8") as f:
                snap = json.load(f)
            break
        except FileNotFoundError as exc:
            return {}, [_say("no_status", error=type(exc).__name__)]
        except (OSError, ValueError) as exc:
            # Windows refuses the open while the tray is replacing the file
            # (CI, 2026-10-30): a moment later it's whole again.
            if attempt == attempts - 1:
                return {}, [_say("no_status", error=type(exc).__name__)]
            time.sleep(_cfg()["read_retry_seconds"])
    blockers = []
    if not isinstance(snap, dict) or "pid" not in snap:
        return {}, [_say("not_snapshot")]
    age = (now or time.time()) - dt.datetime.fromisoformat(snap["written"]).timestamp()
    if not _alive(snap["pid"]):
        blockers.append(_say("exited", pid=snap["pid"]))
    elif age > stale_seconds():
        blockers.append(_say("stale", age=age))
    if snap.get("collect_error"):
        blockers.append(_say("collect_error", error=snap["collect_error"]))
    for t in snap.get("timers") or []:
        blockers.append(_say("timer", label=t.get("label") or _say("unnamed_timer"), ends_at=t.get("ends_at")))
    if (snap.get("study") or {}).get("active"):
        blockers.append(_say("study", phase=snap["study"].get("phase"), ends_at=snap["study"].get("ends_at")))
    if snap.get("chat_busy"):
        blockers.append(_say("chat_busy"))
    # "running" is the job state the tray itself writes, not wording.
    blockers += [_say("job", job=j) for j, s in (snap.get("jobs") or {}).items() if s == "running"]
    return snap, blockers


if __name__ == "__main__":
    snap, blockers = report()
    print(json.dumps(snap, indent=2) if snap else _say("no_snapshot"))
    print("\n" + (_say("unsafe", reasons=_say("reason_separator").join(blockers)) if blockers else _say("safe")))
    sys.exit(0 if not blockers else 1)
