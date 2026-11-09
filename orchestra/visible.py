"""Visible runs: Chuen can see Orchestra working (9 Nov 2026: "I want to see Claude Code
running, not just in the background, because I don't know if it's actually happening").

    lane_started(task, cfg, review, log)           the live window (once) + a start toast
    lane_finished(task, result, cfg, log)          a finish toast
    lane_tab(task, cfg, log_path=..., done_path=..., timeout=..., log=...)
                                                   the lane's own Windows Terminal tab (lane_tab.py)
    lane_tab_done(done_path, outcome, report)      tells that tab the lane has exited

The live window is one console titled cfg["live_window_title"] running orchestra/watch.py.
It is opened when a lane starts and no such window is open (its pid and creation time are
kept in <review>/live_window.json), shown without taking focus, and never opened twice:
one window, reused. Toasts go through orchestra/toast.ps1. Both are switches in
config.json (live_window, toasts). Nothing here can stop a lane: every failure is one log
line. Tests inject `spawn`, `alive` and `run`, so no real window or toast is ever made.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILE = "live_window.json"
CREATE_NEW_CONSOLE = 0x00000010
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
STARTF_USESHOWWINDOW = 0x00000001
SW_SHOWNOACTIVATE = 4  # shown, but the window Chuen is typing in keeps the focus


def console_python() -> str:
    """The interpreter for a console child: python.exe beside a pythonw.exe runner."""
    exe = Path(sys.executable)
    if exe.stem.lower() == "pythonw":
        twin = exe.with_name("python" + exe.suffix)
        if twin.exists():
            return str(twin)
    return str(exe)


def _fill(argv: list, **values) -> list[str]:
    out = []
    for a in argv:
        a = str(a)
        for k, v in values.items():
            a = a.replace("{" + k + "}", str(v))
        out.append(a)
    return out


def spawn_window(argv: list[str], env: dict) -> int:
    """A new, visible console that does not take the focus, outside the runner's job (the
    tick ending must not close it). Returns its pid."""
    kw: dict = {"env": env, "stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name != "nt":
        return subprocess.Popen(argv, start_new_session=True, **kw).pid
    si = subprocess.STARTUPINFO()
    si.dwFlags |= STARTF_USESHOWWINDOW
    si.wShowWindow = SW_SHOWNOACTIVATE
    try:
        return subprocess.Popen(argv, creationflags=CREATE_NEW_CONSOLE | CREATE_BREAKAWAY_FROM_JOB,
                                startupinfo=si, **kw).pid
    except OSError:  # the job forbids breakaway
        return subprocess.Popen(argv, creationflags=CREATE_NEW_CONSOLE, startupinfo=si, **kw).pid


def _created(pid: int):
    """0 if the process exists (creation time unknown), else None. The runner passes its own
    proc_created instead, which also tells a reused pid apart."""
    try:
        from local_team.lease import pid_alive
    except Exception:  # noqa: BLE001
        return None
    return 0 if pid_alive(pid) else None


def window_open(review: Path, alive=None) -> bool:
    """The window recorded in live_window.json is still running (same pid, same creation time)."""
    alive = alive or _created
    try:
        data = json.loads((Path(review) / STATE_FILE).read_text(encoding="utf-8"))
        pid, created = int(data["pid"]), data.get("created")
    except (OSError, ValueError, KeyError, TypeError):
        return False
    now = alive(pid)
    if now is None:
        return False
    return created in (None, 0) or now in (0, created)  # 0: exists, creation time unreadable


SPAWN = spawn_window   # what opens the window; tests replace it (never a real window in a test)
RUN = subprocess.run   # what runs toast.ps1; tests replace it (never a real toast in a test)


def ensure_window(cfg: dict, review: Path, repo: Path, log, spawn=None, alive=None) -> str:
    """Open the live window unless it is off or already open. Returns opened / open / off /
    failed. Never raises."""
    try:
        if not cfg.get("live_window"):
            return "off"
        if window_open(review, alive):
            return "open"
        argv = _fill(cfg["live_window_argv"], python=console_python(), watch=HERE / "watch.py",
                     title=cfg["live_window_title"])
        env = {**os.environ, "ORCHESTRA_REPO": str(repo)}
        pid = (spawn or SPAWN)(argv, env)
        created = (alive or _created)(pid)
        Path(review).mkdir(parents=True, exist_ok=True)
        (Path(review) / STATE_FILE).write_text(json.dumps({"pid": pid, "created": created}), encoding="utf-8")
        log(f"LIVE window opened: {cfg['live_window_title']!r} pid={pid}")
        return "opened"
    except Exception as e:  # noqa: BLE001 -- seeing it is a convenience, never a reason to stop
        try:
            log(f"LIVE window not opened ({type(e).__name__}: {e})")
        except Exception:  # noqa: BLE001
            pass
        return "failed"


def toast(cfg: dict, text: str, log, run=None) -> bool:
    """One Windows toast with `text`. False (and a log line) if it is off or fails. Never raises."""
    if not cfg.get("toasts"):
        return False
    argv = _fill(cfg["toast_argv"], script=HERE / "toast.ps1", text=text)
    try:
        r = (run or RUN)(argv, capture_output=True, text=True, timeout=cfg["toast_timeout_s"],
                                    stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            log(f"TOAST failed (exit {r.returncode}): {' '.join((r.stderr or r.stdout or '').split())[-int(cfg['toast_error_chars']):]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        try:
            log(f"TOAST failed ({type(e).__name__}: {e})")
        except Exception:  # noqa: BLE001
            pass
        return False


WHICH = shutil.which  # finds wt.exe; tests replace it


def tab_title(task, cfg: dict) -> str:
    """'Claude: <task title>' (config lane_tab_title), cut to lane_tab_title_max. ';' would split
    a wt.exe command line into two commands, so it never reaches one."""
    lane = str(task.lane)
    try:
        text = cfg["lane_tab_title"].format(Lane=lane[:1].upper() + lane[1:], lane=lane, title=task.title,
                                            slug=task.slug)
    except (KeyError, IndexError, ValueError):
        text = f"{lane}: {task.title}"
    text = " ".join(text.replace(";", ",").replace('"', "'").split())
    n = int(cfg["lane_tab_title_max"])
    return text if len(text) <= n else text[: max(1, n - 1)] + "…"


def lane_tab(task, cfg: dict, *, log_path: Path, done_path: Path, timeout: int, log, spawn=None,
             which=None) -> str:
    """Open the lane's own tab (lane_tab.py following its lane log). Returns opened / off /
    failed. wt.exe missing: the same renderer in a plain console (lane_tab_fallback_argv), or
    nothing if that is []. Never raises: a lane never waits for, or depends on, its tab."""
    try:
        if not cfg.get("lane_tabs") or task.lane not in cfg["lane_tab_lanes"]:
            return "off"
        Path(done_path).unlink(missing_ok=True)  # an earlier attempt's DONE would close it at once
        title = tab_title(task, cfg)
        values = dict(python=console_python(), script=HERE / "lane_tab.py", log=log_path, done=done_path,
                      tab_title=title, window=cfg["lane_tab_window"], lane=task.lane,
                      max_s=int(timeout) + int(cfg["lane_tab_extra_s"]), linger_s=cfg["lane_tab_linger_s"])
        argv = _fill(cfg["lane_tab_argv"], **values)
        how = "tab"
        if not (which or WHICH)(argv[0]):
            argv = _fill(cfg["lane_tab_fallback_argv"], **values) if cfg["lane_tab_fallback_console"] else []
            how = "console (no " + str(cfg["lane_tab_argv"][0]) + ")"
            if not argv:
                log(f"LANE tab not opened for {task.slug}: {cfg['lane_tab_argv'][0]} not found, no fallback")
                return "failed"
        pid = (spawn or SPAWN)(argv, {**os.environ})
        log(f"LANE tab opened for {task.slug}: {title!r} ({how}) pid={pid}")
        return "opened"
    except Exception as e:  # noqa: BLE001
        try:
            log(f"LANE tab not opened for {task.slug} ({type(e).__name__}: {e})")
        except Exception:  # noqa: BLE001
            pass
        return "failed"


def lane_tab_done(done_path: Path, outcome: str, report: Path | None, log=None) -> None:
    """Tell the tab the lane has exited: the outcome and the report's RESULT line, if any.
    Written atomically, so the tab never reads half a file. Never raises."""
    try:
        result = ""
        if report is not None and Path(report).exists():
            first = (Path(report).read_text(encoding="utf-8", errors="replace").strip().splitlines() or [""])[0]
            result = first.split(":", 1)[-1].strip() if first.upper().startswith("RESULT:") else ""
        done_path = Path(done_path)
        tmp = done_path.with_name(done_path.name + ".tmp")
        tmp.write_text(json.dumps({"outcome": outcome, "result": result or outcome}), encoding="utf-8")
        os.replace(tmp, done_path)
    except Exception as e:  # noqa: BLE001
        if log:
            try:
                log(f"LANE tab DONE not written ({type(e).__name__}: {e})")
            except Exception:  # noqa: BLE001
                pass


def lane_started(task, cfg: dict, review: Path, repo: Path, log, spawn=None, alive=None, run=None) -> None:
    ensure_window(cfg, review, repo, log, spawn, alive)
    try:
        text = cfg["toast_started"].format(lane=task.lane, title=task.title, slug=task.slug)
    except Exception:  # noqa: BLE001
        return
    toast(cfg, text, log, run)


def lane_finished(task, result: str, cfg: dict, log, run=None) -> None:
    try:
        text = cfg["toast_finished"].format(lane=task.lane, title=task.title, slug=task.slug, result=result)
    except Exception:  # noqa: BLE001
        return
    toast(cfg, text, log, run)
