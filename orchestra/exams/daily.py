"""The daily exam run: one canary exam per agent, once a day, in the background.

The runner calls due() and start() each tick (orchestra/runner.py exams_step). start()
claims .review/exams.lock, marks today as run in .review/exams/last_run.json, and spawns

    python -m orchestra.exams.daily --agents codex,copilot,local --scores <main>/.review/exams/scores.json ...

as a hidden, detached process, so it never takes a lane slot and the tick never waits for it.
The child runs `python -m orchestra.exams canary --agent X` per agent (that path records the
scores and rewrites <vault>/Dev/Agent-Scores.md), stops at a total cap (15 min), then logs one
line to runner.log and the vault's Orchestra day note (aftercare.exam_note), and frees the lock.
Stdlib only. Tests drive run_all() with fake agents and due() with a fake clock.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import core

DEFAULT_AT = "07:10"
DEFAULT_AGENTS = ("codex", "copilot", "local")
CAP_S = 900               # the whole run, every agent together
LOCK_STALE_S = CAP_S + 600  # a lock older than this is a dead run's leftover
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def agents_for(cfg: dict) -> list[str]:
    """exams_agents from config; claude only with exams_include_claude: true."""
    names = list(cfg.get("exams_agents") or DEFAULT_AGENTS)
    if not cfg.get("exams_include_claude"):
        names = [n for n in names if n != "claude"]
    return names


def _at_time(raw: str) -> dt.time | None:
    try:
        h, m = str(raw).split(":")
        return dt.time(int(h), int(m))
    except (ValueError, TypeError):
        return None


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def lock_held(lock: Path, at: dt.datetime) -> bool:
    """A lock younger than LOCK_STALE_S is a live run (age only: Windows has no safe pid probe)."""
    info = read_json(lock)
    if not lock.exists():
        return False
    try:
        since = dt.datetime.fromisoformat(info["since"])
    except (KeyError, ValueError, TypeError):
        try:
            since = dt.datetime.fromtimestamp(lock.stat().st_mtime)
        except OSError:
            return False
    since = since.replace(tzinfo=None) if since.tzinfo else since
    return (at - since).total_seconds() < LOCK_STALE_S


def due(cfg: dict, last_run: Path, lock: Path, at: dt.datetime, limited: list[str]) -> str | None:
    """None if an exam run should start now, else why not. `at` is naive local time."""
    when = _at_time(cfg.get("exams_daily_at", ""))
    if when is None:
        return "exams off (no exams_daily_at)"
    if not agents_for(cfg):
        return "no exam agents"
    if at.time() < when:
        return f"before {when:%H:%M}"
    if read_json(last_run).get("day") == at.date().isoformat():
        return "already ran today"
    if limited:
        return "lane limited: " + ", ".join(limited)
    if lock_held(lock, at):
        return "exam run in progress (lock held)"
    return None


def claim(lock: Path, last_run: Path, at: dt.datetime, agents: list[str]) -> str | None:
    """Take the lock (O_EXCL; a stale one is replaced) and mark today. Returns a token, or None
    if another tick got there first."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and not lock_held(lock, at):
        lock.unlink(missing_ok=True)
    token = uuid.uuid4().hex
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"since": at.isoformat(timespec="seconds"), "token": token, "pid": None}, f)
    last_run.parent.mkdir(parents=True, exist_ok=True)
    last_run.write_text(json.dumps({"day": at.date().isoformat(), "started": at.isoformat(timespec="seconds"),
                                    "agents": agents, "finished": None}, indent=1), encoding="utf-8")
    return token


def child_argv(python: str, *, agents: list[str], scores: Path, lock: Path, token: str, last_run: Path,
               log: Path, vault: Path | None, cap: float, include_claude: bool) -> list[str]:
    argv = [python, "-m", "orchestra.exams.daily", "--agents", ",".join(agents), "--scores", str(scores),
            "--lock", str(lock), "--token", token, "--last-run", str(last_run), "--log", str(log),
            "--cap", str(int(cap))]
    if vault:
        argv += ["--vault", str(vault)]
    if include_claude:
        argv.append("--include-claude")
    return argv


def spawn(argv: list[str], cwd: Path, env: dict | None = None) -> int:
    """Start the child detached and hidden; never wait for it. Returns its pid."""
    kw: dict = {"cwd": cwd, "env": env, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        base = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        try:  # out of Task Scheduler's job, so the tick ending can't take it down
            return subprocess.Popen(argv, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kw).pid
        except OSError:  # the job forbids breakaway
            return subprocess.Popen(argv, creationflags=base, **kw).pid
    return subprocess.Popen(argv, start_new_session=True, **kw).pid


# ---------------------------------------------------------------- the child

def _log_line(log: Path | None, msg: str) -> None:
    line = f"{dt.datetime.now().replace(microsecond=0).isoformat()} {msg}"
    print(line)
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def subprocess_agent(scores: Path, vault: Path | None, include_claude: bool):
    """run_agent(name, timeout) -> None: `python -m orchestra.exams canary --agent name` (the existing
    path: it records into scores and rewrites the vault note). A timeout kills its whole tree."""
    def run_agent(name: str, timeout: float) -> None:
        argv = [sys.executable, "-m", "orchestra.exams", "--scores", str(scores)]
        if vault:
            argv += ["--vault", str(vault)]
        argv += ["canary", "--agent", name]
        if include_claude or name == "claude":
            argv.append("--include-claude")
        kw = ({"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
              else {"start_new_session": True})
        proc = subprocess.Popen(argv, cwd=core.REPO, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **kw)
        try:
            proc.wait(timeout=max(1.0, timeout))
        except subprocess.TimeoutExpired:
            core._kill_tree(proc)
            raise TimeoutError(f"canary {name} cut at the {timeout:.0f}s left of the cap")
    return run_agent


def summary(scores: dict, agents: list[str], since: dt.datetime) -> dict[str, str]:
    """name -> 'passed/taken' (or 'skipped: why', 'no result') for results recorded since `since`."""
    out: dict[str, str] = {}
    for want in agents:
        names = sorted(n for n in set(scores.get("agents", {})) | set(scores.get("skipped", {}))
                       if n == want or n.startswith(want + ":"))
        hit = False
        for n in names:
            rows = [r for rows in scores.get("agents", {}).get(n, {}).values() for r in rows
                    if dt.datetime.fromisoformat(r["at"]) >= since]
            if rows:
                out[n] = f"{sum(1 for r in rows if r.get('ok'))}/{len(rows)}"
                hit = True
                continue
            skip = scores.get("skipped", {}).get(n)
            if skip and dt.datetime.fromisoformat(skip["at"]) >= since:
                out[n] = "skipped: " + skip.get("why", "")[:60]
                hit = True
        if not hit:
            out[want] = "no result"
    return out


def run_all(agents: list[str], run_agent, *, scores_file: Path, cap: float = CAP_S,
            clock=time.monotonic, log=None) -> dict:
    """Every agent in turn until the cap runs out. Never raises for one agent's failure."""
    started = core.now().replace(microsecond=0)  # results are stamped to the second
    end = clock() + cap
    notes: dict[str, str] = {}
    for name in agents:
        left = end - clock()
        if left <= 0:
            notes[name] = "not run: cap reached"
            continue
        try:
            run_agent(name, left)
        except Exception as e:  # one broken agent never stops the others
            notes[name] = f"{type(e).__name__}: {e}"[:120]
    res = summary(core.load_scores(scores_file), agents, started)
    for name, why in notes.items():
        if res.get(name) in (None, "no result"):
            res[name] = why
    return res


def line_for(results: dict[str, str], seconds: float) -> str:
    return "Exams: " + "; ".join(f"{n} {r}" for n, r in results.items()) + f" ({round(seconds)}s)"


def _release(lock: Path, token: str | None) -> None:
    info = read_json(lock)
    if token is None or info.get("token") == token:
        lock.unlink(missing_ok=True)


def child_main(argv: list[str] | None = None, run_agent=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m orchestra.exams.daily")
    ap.add_argument("--agents", default=",".join(DEFAULT_AGENTS))
    ap.add_argument("--scores", required=True)
    ap.add_argument("--lock", required=True)
    ap.add_argument("--token", help="the runner already took the lock with this token")
    ap.add_argument("--last-run")
    ap.add_argument("--log")
    ap.add_argument("--vault")
    ap.add_argument("--cap", type=float, default=CAP_S)
    ap.add_argument("--include-claude", action="store_true")
    a = ap.parse_args(argv)
    lock, scores = Path(a.lock), Path(a.scores)
    log = Path(a.log) if a.log else None
    vault = Path(a.vault) if a.vault else None
    agents = [n for n in a.agents.split(",") if n]
    token = a.token
    at = dt.datetime.now().replace(microsecond=0)
    if token:
        if read_json(lock).get("token") != token:
            _log_line(log, "EXAMS not run: the lock is not ours any more")
            return 1
    else:  # run by hand: take the lock ourselves
        if lock_held(lock, at):
            _log_line(log, "EXAMS not run: another exam run holds the lock")
            return 1
        lock.unlink(missing_ok=True)
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            return 1
        token = uuid.uuid4().hex
    lock.write_text(json.dumps({"since": at.isoformat(timespec="seconds"), "token": token, "pid": os.getpid()}),
                    encoding="utf-8")
    t0 = time.monotonic()
    try:
        _log_line(log, f"EXAMS start agents={','.join(agents)} cap={a.cap:.0f}s pid={os.getpid()}")
        results = run_all(agents, run_agent or subprocess_agent(scores, vault, a.include_claude),
                          scores_file=scores, cap=a.cap)
        line = line_for(results, time.monotonic() - t0)
        _log_line(log, "EXAMS done " + line)
        if a.last_run:
            p = Path(a.last_run)
            rec = read_json(p) or {"day": at.date().isoformat(), "started": at.isoformat(timespec="seconds")}
            rec.update(finished=dt.datetime.now().isoformat(timespec="seconds"), results=results, line=line)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(rec, indent=1), encoding="utf-8")
        if vault and vault.is_dir():
            try:
                sys.path.insert(0, str(core.HERE.parent))
                import aftercare
                aftercare.exam_note(vault, line)
            except Exception as e:  # the day note must never break the run
                _log_line(log, f"EXAMS day note failed: {e!r}")
        return 0
    finally:
        _release(lock, token)


if __name__ == "__main__":
    sys.exit(child_main())
