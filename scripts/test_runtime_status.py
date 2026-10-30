"""The running tray's status snapshot, and the restart verdict read from it.

Temporary status files only; the tray's own collector runs against fake
timers and study state in this process. No tray is started.

    .venv\\Scripts\\python.exe scripts\\test_runtime_status.py
"""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import runtime_status  # noqa: E402

results = []
TMP = tempfile.mkdtemp(prefix="denisse-runtime-")


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
check("code version is the checked-out commit, read without git", head.startswith(runtime_status.code_version()),
      runtime_status.code_version())

path = os.path.join(TMP, "s.json")
state = {"timers": [], "study": {"active": False}, "chat_busy": False, "jobs": {"downloads": "completed"}}
once = runtime_status.start(lambda: dict(state), path=path, every=3600)
once()
snap, blockers = runtime_status.report(path)
check("idle tray: safe to restart", blockers == [] and snap["pid"] == os.getpid(), blockers)
check("snapshot carries pid, start time and loaded code", {"pid", "started", "code", "written"} <= set(snap))

for change, words in (({"timers": [{"label": "tea", "ends_at": "2026-10-30T23:30:00"}]}, "timer running: tea"),
                      ({"study": {"active": True, "phase": "study", "ends_at": "2026-10-30T23:40:00"}}, "study mode on"),
                      ({"chat_busy": True}, "a reply is being worked on"),
                      ({"jobs": {"downloads": "running"}}, "job running: downloads")):
    state.update(change)
    once()
    _, blockers = runtime_status.report(path)
    check(f"blocked: {words}", any(words in b for b in blockers), blockers)
    state.update({"timers": [], "study": {"active": False}, "chat_busy": False, "jobs": {"downloads": "completed"}})

bad = runtime_status.start(lambda: 1 / 0, path=path, every=3600)
bad()
_, blockers = runtime_status.report(path)
check("a collector that fails is reported, not trusted as idle", any("couldn't read its own state" in b for b in blockers),
      blockers)

once()
with open(path, encoding="utf-8") as f:
    snap = json.load(f)
old = dict(snap, written=(dt.datetime.now().astimezone() - dt.timedelta(minutes=5)).isoformat())
with open(path, "w", encoding="utf-8") as f:
    json.dump(old, f)
_, blockers = runtime_status.report(path)
check("an old snapshot isn't trusted (hung tray)", any("old" in b for b in blockers), blockers)
dead = subprocess.Popen([sys.executable, "-c", "pass"])
dead.wait()
with open(path, "w", encoding="utf-8") as f:
    json.dump(dict(snap, pid=dead.pid), f)
_, blockers = runtime_status.report(path)
check("a snapshot from an exited process isn't trusted", any("isn't running" in b for b in blockers), blockers)
_, blockers = runtime_status.report(os.path.join(TMP, "missing.json"))
check("no snapshot: says so, not 'safe'", blockers and "no readable status" in blockers[0], blockers)

# The tray's own collector, against fake in-memory state.
try:
    import tray
except Exception as exc:  # noqa: BLE001
    check("tray imports for its collector", False, f"{type(exc).__name__}: {exc}")
else:
    import timers

    class Study:
        active, phase = True, "rest"
        ends_at = dt.datetime(2026, 10, 30, 23, 45)

    with timers._lock:
        saved = dict(timers._timers)
        timers._timers.clear()
        timers._timers[99] = {"id": 99, "label": "pasta", "ends_at": dt.datetime(2026, 10, 30, 23, 20), "minutes": 9}
    real_study, real_engine = tray._study_mode, tray._get_agent_engine
    tray._study_mode = Study()
    tray._get_agent_engine = lambda: type("E", (), {"status": lambda self: {"last_runs": {"downloads": {"status": "running"}}}})()
    tray._thinking_sources.add("chat")
    try:
        got = tray._runtime_state()
    finally:
        tray._thinking_sources.discard("chat")
        tray._study_mode, tray._get_agent_engine = real_study, real_engine
        with timers._lock:
            timers._timers.clear()
            timers._timers.update(saved)
    check("tray collector: timers, study, chat and jobs from memory",
          got == {"timers": [{"label": "pasta", "ends_at": "2026-10-30T23:20:00"}],
                  "study": {"active": True, "phase": "rest", "ends_at": "2026-10-30T23:45:00"},
                  "chat_busy": True, "jobs": {"downloads": "running"}}, got)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
