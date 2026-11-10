"""Master's own health: is the baseline check green on the commit master is at right now?

Every task's gate includes run_checks.py --fast, so one test that is red on master made every
session report PARTIAL, whatever it worked on (4 Nov 2026: a config default out of step, six
tasks; 5 Nov: a test that failed on Thursdays, seven; audit 10 Nov 2026, H1). The runner could
not tell "this branch broke a test" from "that test was already broken on master".

This module keeps, in .review/<master_health_file>, one record per master commit:
    {"status": "green" | "red" | "broken", "modules": [...], "tail": "...", "checked": ...,
     "seconds": n, "how": "baseline" | "combined"}
and the runner uses it in four places (one call each):
  * step()        once a tick: a master commit with no record gets a baseline run in a detached
                  background process (like the daily exams: no lane slot, the tick never waits);
                  never on battery, never while paused, one at a time (its own lock). While the
                  record is red: one throttled log line, and one urgent bug-fix task written
                  through new_task.create() if no bug-fix task is queued.
  * hold()        why_not_runnable(): while master is red, every task but a bug fix waits.
  * after_merge() integrate(), right after the fast-forward: the commit the combined tree just
                  tested is recorded green without a second run, when those checks covered the
                  baseline.
  * park_note()   park(): when the branch's base commit was red, the park reason says so, and
                  whether the runner's gate failed only on modules already red there.
Not checked yet counts as green: an unknown commit never holds anything.

The child (the baseline run itself):
    .venv\\Scripts\\python.exe orchestra\\master_health.py --commit <sha> --token <token>
Every command, wording and limit is in orchestra/config.json (master_health_*), with its reason.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
_HEAD_CACHE: dict = {}


# ---------------------------------------------------------------- the record file

def _file(R) -> Path:
    return R.REVIEW / R.C["master_health_file"]


def _lock_file(R) -> Path:
    return R.REVIEW / R.C["master_health_lock"]


def load(R) -> dict:
    try:
        data = json.loads(_file(R).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"records": {}}
    if not isinstance(data, dict) or not isinstance(data.get("records"), dict):
        return {"records": {}}
    return data


def _update(R, change) -> dict:
    """Read, change and write the record file under a short lock of its own (the runner and the
    background child both write it). Atomic replace, so a reader never sees half a file."""
    path = _file(R)
    path.parent.mkdir(parents=True, exist_ok=True)
    with R._FileLock(path.with_suffix(path.suffix + ".write.lock"), wait=30, stale=60):
        data = load(R)
        change(data)
        recs = data["records"]
        keep = int(R.C["master_health_keep"])
        if len(recs) > keep:  # oldest checks out first
            for sha in sorted(recs, key=lambda s: str(recs[s].get("checked", "")))[: len(recs) - keep]:
                del recs[sha]
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    return data


def record(R, commit: str) -> dict | None:
    return load(R)["records"].get(commit) if commit else None


def due_again(R, rec: dict) -> bool:
    """A broken record (timed out, or failed without naming a module) is checked again after
    master_health_broken_retry_min: it may have been the machine, not master."""
    if rec.get("status") != "broken":
        return False
    try:
        when = dt.datetime.fromisoformat(rec.get("checked") or "")
    except ValueError:
        return True
    return (R.now() - when).total_seconds() >= 60 * float(R.C["master_health_broken_retry_min"])


def save_record(R, commit: str, rec: dict) -> None:
    def change(data):
        old = data["records"].get(commit) or {}
        data["records"][commit] = {**{k: old[k] for k in ("bugfix_task", "logged") if k in old}, **rec}
    _update(R, change)


def head(R) -> str:
    """The commit master is at (the runner's own meaning: HEAD of the main checkout), cached for
    master_health_head_cache_s so a tick's why_not_runnable() calls cost one git call, not one each."""
    ttl = float(R.C["master_health_head_cache_s"])
    key = str(R.REPO)
    hit = _HEAD_CACHE.get(key)
    if hit and ttl > 0 and time.monotonic() - hit[1] < ttl:
        return hit[0]
    r = R.git("rev-parse", "HEAD", check=False)
    sha = r.stdout.strip() if r.returncode == 0 else ""
    _HEAD_CACHE[key] = (sha, time.monotonic())
    return sha


def forget_head() -> None:
    _HEAD_CACHE.clear()


# ---------------------------------------------------------------- reading run_checks' output

def failing_modules(R, output: str) -> list[str]:
    """Modules run_checks says did not pass, from its own per-module lines
    (master_health_module_patterns, each with (?P<module>) and (?P<status>) groups). Statuses in
    master_health_pass_statuses count as passed. Warnings and other noise are never modules."""
    passing = {s.upper() for s in R.C["master_health_pass_statuses"]}
    found = []
    for pat in R.C["master_health_module_patterns"]:
        for m in re.finditer(pat, output, re.M):
            mod, status = m.group("module"), m.group("status").upper()
            if status not in passing and mod not in found:
                found.append(mod)
    return found


def describe_modules(R, rec: dict) -> str:
    mods = rec.get("modules") or []
    return ", ".join(mods) if mods else R.C["master_health_unparsed"]


def judge(R, code: int | None, output: str) -> dict:
    """One baseline command's result -> {status, modules, tail}."""
    tail = "\n".join(output.strip().splitlines()[-int(R.C["master_health_tail_lines"]):])
    if code is None or code in R.C["broken_gate_codes"]:
        return {"status": "broken", "modules": [], "tail": tail, "exit": code}
    if code == 0:
        return {"status": "green", "modules": [], "tail": "", "exit": 0}
    mods = failing_modules(R, output)
    # No module named: the check may not have run at all (a missing venv link, a bad path: cmd.exe
    # says "The system cannot find the path specified" and exits 1). Holding every task on that
    # would stall the queue on the environment, not on master: broken, unless config says hold.
    status = "red" if mods or R.C["master_health_unparsed_holds"] else "broken"
    return {"status": status, "modules": mods, "tail": tail, "exit": code}


# ---------------------------------------------------------------- the baseline run (the child)

def baseline(R) -> list[str]:
    return list(R.C["master_health_baseline"] or R.C["hub_queue_default_gate"])


def run_check(R, commit: str, token: str | None = None) -> dict:
    """Run the baseline commands in a detached worktree of `commit` (made and removed the way
    integration_tree() does, venv linked) and record the result. Releases the lock if `token`
    is the one holding it. Returns the record."""
    start = time.monotonic()
    wt = R.WORKTREES / f"{R.C['master_health_worktree_prefix']}{commit[:12]}"
    log_path = R.REPORTS / R.C["master_health_log_file"]
    try:
        if wt.exists():
            R.remove_worktree(wt, "", delete_branch=False)
        R.git("worktree", "prune", check=False)
        R.WORKTREES.mkdir(parents=True, exist_ok=True)
        R.git("worktree", "add", "--detach", str(wt), commit)
        R.link_venv(wt)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
        result = {"status": "green", "modules": [], "tail": ""}
        try:
            for cmd in baseline(R):
                code, output = R.run_bounded(cmd, wt, log_path, R.C["master_health_timeout_s"])
                result = judge(R, code, output)
                result["command"] = cmd
                if result["status"] != "green":
                    break
        finally:
            R.remove_worktree(wt, "", delete_branch=False)
        rec = {**result, "checked": R.now().isoformat(), "seconds": round(time.monotonic() - start, 1),
               "how": "baseline"}
        save_record(R, commit, rec)
        R.log(R.C["master_health_done_log"].format(
            commit=commit[:8], status=rec["status"], seconds=rec["seconds"],
            detail="" if rec["status"] == "green" else f": {describe_modules(R, rec)}"))
        return rec
    finally:
        release(R, token)


def _since(info: dict) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(info["since"])
    except (KeyError, TypeError, ValueError):
        return None


def lock_held(R) -> bool:
    """A baseline run holds the lock; one older than its timeout plus master_health_lock_grace_s
    is stale (the child died without releasing it)."""
    try:
        info = json.loads(_lock_file(R).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        info = {}
    since = _since(info if isinstance(info, dict) else {})
    if since is None:
        try:
            since = dt.datetime.fromtimestamp(_lock_file(R).stat().st_mtime).replace(microsecond=0)
        except OSError:
            return False
    limit = float(R.C["master_health_timeout_s"]) * max(1, len(baseline(R))) + float(R.C["master_health_lock_grace_s"])
    return (R.now() - since).total_seconds() < limit


def claim(R, commit: str) -> str | None:
    """Take the lock (O_EXCL; a stale one is replaced). A token, or None if a run holds it."""
    lock = _lock_file(R)
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and not lock_held(R):
        R.unlink_retry(lock)
    token = uuid.uuid4().hex
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"since": R.now().isoformat(), "token": token, "commit": commit}, f)
    return token


def release(R, token: str | None) -> None:
    if not token:
        return
    try:
        info = json.loads(_lock_file(R).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(info, dict) and info.get("token") == token:
        R.unlink_retry(_lock_file(R))


def child_argv(R, commit: str, token: str) -> list[str]:
    return [sys.executable, str(HERE / "master_health.py"), "--commit", commit, "--token", token]


def spawn(argv: list[str], env: dict) -> int:
    """Detached and hidden, out of Task Scheduler's job: the same start as the daily exams."""
    from exams import daily
    return daily.spawn(argv, cwd=HERE.parent, env=env)


# ---------------------------------------------------------------- the runner's calls

def enabled(R) -> bool:
    """Config master_health, and the real repo only (tests patch this, as exams_enabled)."""
    return bool(R.C["master_health"]) and R.aftercare_on()


def red(R, commit: str | None = None) -> dict | None:
    """The record for `commit` (default: master now) if it is red, else None. Unknown is green."""
    commit = commit if commit is not None else head(R)
    rec = record(R, commit)
    return rec if rec and rec.get("status") == "red" else None


def hold(R, task) -> str | None:
    """why_not_runnable(): while master is red every task but a bug fix waits. Never raises."""
    try:
        if not R.C["master_health"] or R.is_bugfix(task):
            return None
        rec = red(R)
        return R.C["master_health_hold"].format(modules=describe_modules(R, rec)) if rec else None
    except Exception:  # a broken record file must never stop dispatch
        return None


def step(R, state: dict, power_status=None, spawn_fn=None) -> str:
    """Once a tick. Starts a baseline run for an unchecked master commit; while master is red,
    logs it (throttled) and makes sure one bug-fix task is queued. Returns what it did, for the
    tests. Never raises: master's health must never stop a tick from dispatching."""
    try:
        if not enabled(R):
            return "off"
        import power
        why = R.campaign.dispatch_block(R.REVIEW) or power.dispatch_block(
            power_status if power_status is not None else power.power_status(), R.C)
        if why:
            return f"not now: {why}"
        commit = head(R)
        if not commit:
            return "no master commit"
        rec = record(R, commit)
        if rec is None or due_again(R, rec):
            return _start(R, commit, spawn_fn)
        if rec.get("status") != "red":
            return rec.get("status", "?")
        _log_red(R, commit, rec)
        return "red: " + _ensure_bugfix(R, commit, rec)
    except Exception as e:
        try:
            R.log(f"master_health: {e!r}")
        except Exception:
            pass
        return f"error: {e!r}"


def _start(R, commit: str, spawn_fn=None) -> str:
    if lock_held(R):
        return "not started: a baseline run is in progress"
    token = claim(R, commit)
    if not token:
        return "not started: another tick took the lock"
    env = {**os.environ}
    try:
        pid = (spawn_fn or spawn)(child_argv(R, commit, token), env)
    except Exception:
        release(R, token)
        raise
    R.log(R.C["master_health_start_log"].format(commit=commit[:8], pid=pid))
    return f"started {pid}"


def _log_red(R, commit: str, rec: dict) -> None:
    try:
        last = dt.datetime.fromisoformat(rec.get("logged") or "")
    except ValueError:
        last = None
    if last is not None and (R.now() - last).total_seconds() < float(R.C["master_health_log_every_s"]):
        return
    R.log(R.C["master_health_red_log"].format(commit=commit[:8], modules=describe_modules(R, rec),
                                              kinds=", ".join(R.C["bugfix_kinds"]) or "none"))

    def change(data):
        data["records"].setdefault(commit, dict(rec))["logged"] = R.now().isoformat()
    _update(R, change)


def _open_bugfix(R) -> str | None:
    for t in R.load_queue():
        if t.auto and R.is_bugfix(t):
            return t.slug
    return None


def _ensure_bugfix(R, commit: str, rec: dict) -> str:
    """One auto, urgent bug-fix task per red commit, and never while a bug fix is queued."""
    open_one = _open_bugfix(R)
    if open_one:
        return f"bug fix queued: {open_one}"
    if rec.get("bugfix_task"):
        return f"bug fix already written for this commit: {rec['bugfix_task']}"
    if not R.C["bugfix_kinds"]:
        return "no bugfix_kinds: no bug-fix task can be written"
    import new_task
    mods = describe_modules(R, rec)
    fill = {"commit": commit, "short": commit[:8], "modules": mods,
            "tail": rec.get("tail") or "(none)", "command": rec.get("command") or "; ".join(baseline(R))}
    try:
        path = new_task.create(R.C["master_health_task_title"].format(**fill),
                               body=R.C["master_health_task_body"].format(**fill), gate=baseline(R),
                               priority=R.C["master_health_task_priority"], weight=R.C["master_health_task_weight"],
                               kind=R.C["bugfix_kinds"][0], lane=R.C["bugfix_lane"] or None, auto=True,
                               source=R.C["master_health_task_source"], review=R.REVIEW)
        written, out = path.stem, f"wrote {path.stem}"
        R.log(R.C["master_health_task_log"].format(task=path.stem, commit=commit[:8], modules=mods))
    except (OSError, ValueError, KeyError) as e:  # no TEMPLATE.md, a bad template: once, not every tick
        written, out = f"not written: {e!r}", f"could not write a bug-fix task: {e!r}"
        R.log(f"MASTER RED {commit[:8]}: {out}")

    def change(data):
        data["records"].setdefault(commit, dict(rec))["bugfix_task"] = written
    _update(R, change)
    return out


def after_merge(R, commit: str, checks: list[str]) -> None:
    """integrate(), right after master's fast-forward to `commit`: when the combined-tree checks
    include one starting with master_health_combined_marks_green, record `commit` green without
    a second run. Otherwise the next tick finds it unchecked and starts a baseline run. Never raises."""
    try:
        mark = R.C["master_health_combined_marks_green"]
        if not R.C["master_health"] or not mark or not any(str(c).startswith(mark) for c in checks):
            return
        save_record(R, commit, {"status": "green", "modules": [], "tail": "", "checked": R.now().isoformat(),
                                "seconds": 0, "how": "combined"})
    except Exception as e:
        try:
            R.log(f"master_health after merge: {e!r}")
        except Exception:
            pass


def park_note(R, task, report) -> str:
    """Text for the end of a park reason when the branch's base commit was red on master's record,
    else "". Says whether the runner's gate (its log newer than the report, so from this run)
    failed only on modules already red there. Never raises."""
    try:
        if not R.C["master_health"]:
            return ""
        branch = f"refs/heads/review/{task.slug}"
        r = R.git("merge-base", "HEAD", branch, check=False)
        base = r.stdout.strip() if r.returncode == 0 else ""
        rec = red(R, base) if base else None
        if not rec:
            return ""
        note = R.C["master_health_park_note"].format(commit=base[:8], modules=describe_modules(R, rec))
        gate_log = R.REPORTS / f"{task.slug}.gate.log"
        fresh = gate_log.exists() and (not (report and Path(report).exists())
                                       or gate_log.stat().st_mtime >= Path(report).stat().st_mtime)
        if fresh and rec.get("modules"):
            failed = failing_modules(R, gate_log.read_text(encoding="utf-8", errors="replace"))
            if failed and set(failed) <= set(rec["modules"]):
                note += R.C["master_health_park_only"]
        return note
    except Exception:
        return ""


def state_line(R) -> str | None:
    """STATE_NOW's line about master: red, a run in progress, or nothing worth a line."""
    try:
        if not R.C["master_health"]:
            return None
        commit = head(R)
        rec = record(R, commit)
        if rec and rec.get("status") == "red":
            return R.C["master_health_state_red"].format(commit=commit[:8], modules=describe_modules(R, rec),
                                                         task=rec.get("bugfix_task") or "none yet")
        if rec:
            return R.C["master_health_state_known"].format(commit=commit[:8], status=rec.get("status"),
                                                           how=rec.get("how", "?"), seconds=rec.get("seconds", "?"))
        return R.C["master_health_state_unknown"].format(
            commit=commit[:8], running="running now" if lock_held(R) else "not started")
    except Exception as e:
        return f"**Master:** unknown ({e})"


# ---------------------------------------------------------------- the child entry point

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run master's baseline check once and record it.")
    ap.add_argument("--commit", required=True)
    ap.add_argument("--token", default=None, help="the runner took the lock with this token")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(HERE))
    import runner as R
    if a.token:
        try:
            info = json.loads(_lock_file(R).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        if not isinstance(info, dict) or info.get("token") != a.token:
            R.log("master_health: not run, the lock is not ours any more")
            return 1
    try:
        rec = run_check(R, a.commit, a.token)
    except Exception as e:
        R.log(f"master_health: baseline run on {a.commit[:8]} crashed: {e!r}")
        release(R, a.token)
        return 1
    return 0 if rec["status"] == "green" else 1


if __name__ == "__main__":
    sys.exit(main())
