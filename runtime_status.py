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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(BASE_DIR, "memory", "runtime_status.json")
EVERY_SECONDS = 10
STALE_SECONDS = 3 * EVERY_SECONDS


def code_version(root=BASE_DIR):
    """The commit checked out when this was called -- read from .git, no
    subprocess. The tray calls it once at startup: what it loaded."""
    try:
        git = os.path.join(root, ".git")
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if not head.startswith("ref: "):
            return head[:12]
        ref = head[5:]
        try:
            with open(os.path.join(git, *ref.split("/")), encoding="utf-8") as f:
                return f.read().strip()[:12]
        except FileNotFoundError:
            with open(os.path.join(git, "packed-refs"), encoding="utf-8") as f:
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


def start(collect, path=None, every=EVERY_SECONDS):
    """Write {pid, started, code, written, **collect()} every `every`
    seconds on a daemon thread. A failing collect is recorded, not raised:
    the tray must never stop because its status report did."""
    path = path or STATUS_FILE
    fixed = {"pid": os.getpid(), "started": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
             "code": code_version()}

    def once():
        try:
            state = collect()
        except Exception as exc:  # noqa: BLE001
            state = {"collect_error": f"{type(exc).__name__}: {exc}"[:200]}
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
    for attempt in range(10):
        try:
            with open(path, encoding="utf-8") as f:
                snap = json.load(f)
            break
        except FileNotFoundError as exc:
            return {}, [f"no readable status ({type(exc).__name__}) -- this tray predates runtime_status, or isn't running"]
        except (OSError, ValueError) as exc:
            # Windows refuses the open while the tray is replacing the file
            # (CI, 2026-10-30): a moment later it's whole again.
            if attempt == 9:
                return {}, [f"no readable status ({type(exc).__name__}) -- this tray predates runtime_status, or isn't running"]
            time.sleep(0.05)
    blockers = []
    if not isinstance(snap, dict) or "pid" not in snap:
        return {}, ["the status file isn't a status snapshot"]
    age = (now or time.time()) - dt.datetime.fromisoformat(snap["written"]).timestamp()
    if not _alive(snap["pid"]):
        blockers.append(f"process {snap['pid']} isn't running -- the snapshot is from a tray that has exited")
    elif age > STALE_SECONDS:
        blockers.append(f"the snapshot is {age:.0f}s old -- the tray may be hung")
    if snap.get("collect_error"):
        blockers.append("the tray couldn't read its own state: " + snap["collect_error"])
    for t in snap.get("timers") or []:
        blockers.append(f"timer running: {t.get('label') or 'unnamed'} until {t.get('ends_at')}")
    if (snap.get("study") or {}).get("active"):
        blockers.append(f"study mode on: {snap['study'].get('phase')} until {snap['study'].get('ends_at')}")
    if snap.get("chat_busy"):
        blockers.append("a reply is being worked on")
    blockers += [f"job running: {j}" for j, s in (snap.get("jobs") or {}).items() if s == "running"]
    return snap, blockers


if __name__ == "__main__":
    snap, blockers = report()
    print(json.dumps(snap, indent=2) if snap else "(no snapshot)")
    print("\nSafe to restart: " + ("yes" if not blockers else "no -- " + "; ".join(blockers)))
    sys.exit(0 if not blockers else 1)
