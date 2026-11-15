"""Game mode: one switch that frees the laptop for a game, and puts everything back after.

    .venv\\Scripts\\python.exe -m orchestra.gamemode status
    .venv\\Scripts\\python.exe -m orchestra.gamemode on
    .venv\\Scripts\\python.exe -m orchestra.gamemode off

(also the hub's "Game mode" button and its "g" key: atrium/live.py.) Chuen, 15 Nov 2026: "add a
pause everything button in the hub, make sure nothing in the background is running, and I can
turn it off and on whenever I play games".

ON first writes .review/<state_file> (git-ignored), so every tick that starts from here on does
nothing, then, recording each step there as it goes:
  a) campaign.pause: no new tasks (left as it is if Chuen had already paused);
  b) the running lanes are stopped the way emergency-stop does it (only process trees named in
     the runner's own lock files, each pid checked against its recorded creation time) and
     parked as "paused for game mode", branch and worktree kept;
  c) every model loaded in Ollama is unloaded (local_llm.unload, keep_alive 0); the server is
     stopped too only when config stop_ollama_server says so. Don's consults are skipped;
  d) Denisse's own scheduled tasks (config scheduled_tasks) that are installed and enabled are
     disabled, so none of them starts a process (or flashes a window) mid-game.
Nothing that isn't Denisse's is touched: no other apps, no Windows settings.

OFF undoes exactly what ON did: resume (unless Chuen had paused before; refused after a campaign
deadline exactly as `campaign.py resume` is), requeue the parked tasks (a full re-run; the runner
keeps their earlier commits under a keep ref and names it in the prompt), restart Ollama only if
ON stopped it, re-enable only the tasks ON disabled. ON twice or OFF twice changes nothing.

Every word, command and switch: orchestra/config.json "gamemode". status() only reads one small
file, so the runner, the night job and the hub page can call it on every tick or poll.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_CODE = HERE.parent


def _config() -> dict:
    """The "gamemode" section: config_defaults.json's, with config.json's keys on top."""
    out: dict = {}
    for name in ("config_defaults.json", "config.json"):
        try:
            sec = json.loads((HERE / name).read_text(encoding="utf-8")).get("gamemode")
        except (OSError, ValueError):
            continue
        if isinstance(sec, dict):
            out.update({k: v for k, v in sec.items() if not k.startswith("_")})
    return out


CFG = _config()


def default_review() -> Path:
    """The runner's own rule (orchestra/runner.py REVIEW): ORCHESTRA_REPO, else this checkout."""
    return Path(os.environ.get("ORCHESTRA_REPO", REPO_CODE)) / ".review"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- the state file

def state_path(review: Path | None = None) -> Path:
    return Path(review or default_review()) / CFG["state_file"]


def status(review: Path | None = None) -> dict:
    """What .review/<state_file> says: {"on": False} when there is none or it can't be read."""
    try:
        data = json.loads(state_path(review).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"on": False}
    return data if isinstance(data, dict) and "on" in data else {"on": False}


def is_on(review: Path | None = None) -> bool:
    return bool(status(review).get("on"))


def _save(review: Path, data: dict) -> None:
    path = state_path(review)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".gm.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- the moving parts (tests replace them)

def _root_on_path() -> None:
    """no_window.py and local_llm.py live at the repo root; appended, so nothing is shadowed."""
    if str(REPO_CODE) not in sys.path:
        sys.path.append(str(REPO_CODE))


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    _root_on_path()
    import no_window
    return subprocess.run(argv, capture_output=True, text=True, errors="replace",
                          timeout=CFG["task_timeout_s"], creationflags=no_window.flags())


def _argv(key: str, name: str) -> list[str]:
    return [a.format(name=name) for a in CFG[key]]


def task_enabled(name: str) -> bool | None:
    """True / False for an installed scheduled task, None when it isn't installed (or can't be read)."""
    try:
        r = _run(_argv("task_query_argv", name))
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return CFG["task_disabled_word"].lower() not in (r.stdout or "").lower()


def set_task(name: str, enabled: bool) -> bool:
    try:
        r = _run(_argv("task_enable_argv" if enabled else "task_disable_argv", name))
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _local_llm():
    _root_on_path()
    import local_llm
    return local_llm


def loaded_models() -> tuple[bool, list[str]]:
    """(Ollama answers, the models it has loaded), from /api/ps."""
    ps, err = _local_llm().read_ps()
    if err is not None or not isinstance(ps, dict):
        return False, []
    return True, [m.get("name") or m.get("model") for m in ps.get("models") or [] if m.get("name") or m.get("model")]


def unload_model(name: str) -> None:
    _local_llm().unload(name, job=CFG["ollama_job"])


def _ollama_plan() -> dict:
    ll = _local_llm()
    return (ll.config()["wedge"].get("restart") or {}).get(sys.platform) or {}


def stop_ollama() -> bool:
    plan = _ollama_plan()
    if not plan.get("kill"):
        return False
    for argv in plan["kill"]:
        try:
            _run(list(argv))
        except (OSError, subprocess.SubprocessError):
            pass
    return True


def start_ollama() -> bool:
    start = [os.path.expandvars(a) for a in _ollama_plan().get("start") or []]
    if not start:
        return False
    _root_on_path()
    import no_window
    try:
        subprocess.Popen(start, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | no_window.flags())
    except OSError:
        return False
    return True


def _modules():
    """(runner, campaign, requeue) as the runner itself imports them, so a test's reloaded runner
    and its patched campaign functions are the ones used."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import campaign
    import runner
    import requeue
    return runner, campaign, requeue


def _log(R, msg: str) -> None:
    try:
        R.log(f"gamemode: {msg}")
    except Exception:  # noqa: BLE001 -- the log is a record; never the reason a switch fails
        pass


def _refresh_state_now() -> None:
    try:
        import state_now
        state_now.write()
    except Exception:  # noqa: BLE001 -- STATE NOW is a view
        pass


# ---------------------------------------------------------------- on / off

def on(R=None, now: dt.datetime | None = None) -> dict:
    """Turn game mode on (see the module doc). Returns the state, with "lines" saying what was done."""
    runner_mod, campaign, _ = _modules()
    R = R or runner_mod
    review = R.REVIEW
    now = now or utcnow()
    st = status(review)
    if st.get("on"):
        return dict(st, lines=["game mode is already on; nothing changed"])
    st = {"on": True, "since": iso(now), "was_paused": bool(campaign.dispatch_block(review)),
          "paused": [], "ollama": {}, "tasks_disabled": [], "lines": []}
    _save(review, st)  # first: a tick starting from now does nothing
    lines = st["lines"]
    if not st["was_paused"]:
        campaign.pause(review, now)
        lines.append("Orchestra paused")
    halted, parked = campaign.halt_runs(R, CFG["paused_result"], CFG["paused_status"], CFG["paused_status"])
    st["paused"] = parked
    lines += halted
    _save(review, st)
    if CFG["unload_ollama"]:
        try:
            up, models = loaded_models()
        except Exception as e:  # noqa: BLE001 -- the rest of game mode still happens
            up, models = False, []
            lines.append(f"Ollama not read: {type(e).__name__}: {e}")
        gone = []
        for m in models:
            try:
                unload_model(m)
                gone.append(m)
            except Exception as e:  # noqa: BLE001
                lines.append(f"could not unload {m}: {type(e).__name__}: {e}")
        st["ollama"] = {"was_running": up, "unloaded": gone, "stopped": False}
        if gone:
            lines.append(f"unloaded {', '.join(gone)}")
        if up and CFG["stop_ollama_server"]:
            st["ollama"]["stopped"] = stop_ollama()
            if st["ollama"]["stopped"]:
                lines.append("Ollama server stopped")
        _save(review, st)
    for name in CFG["scheduled_tasks"]:
        if task_enabled(name) and set_task(name, False):
            st["tasks_disabled"].append(name)
            lines.append(f"scheduled task {name!r} disabled")
            _save(review, st)
    _save(review, st)
    _log(R, f"ON: {'; '.join(lines) or 'nothing was running'}")
    _refresh_state_now()
    return st


def off(R=None, now: dt.datetime | None = None) -> dict:
    """Turn game mode off: undo exactly what on() recorded. Returns the new state, with "lines"."""
    runner_mod, campaign, requeue = _modules()
    R = R or runner_mod
    review = R.REVIEW
    now = now or utcnow()
    st = status(review)
    if not st.get("on"):
        return dict(st, lines=["game mode is already off; nothing changed"])
    lines: list[str] = []
    if not st.get("was_paused"):
        ok, msg = campaign.resume(review, now)
        lines.append(msg if ok else f"Orchestra stays paused: {msg}")
    note = CFG["requeue_note"].format(since=st.get("since", "?"))
    for slug in st.get("paused") or []:
        try:
            requeue.requeue(slug, note)
            lines.append(f"requeued {slug}")
        except requeue.RequeueError as e:  # already put back by hand, or running again: leave it
            lines.append(f"{slug} not requeued: {e}")
    oll = st.get("ollama") or {}
    if oll.get("stopped"):
        lines.append("Ollama started again" if start_ollama() else "Ollama could not be started: start it by hand")
    for name in st.get("tasks_disabled") or []:
        lines.append(f"scheduled task {name!r} " + ("enabled again" if set_task(name, True)
                                                    else "could NOT be enabled again: enable it by hand"))
    out = {"on": False, "last": {k: st.get(k) for k in ("since", "paused", "ollama", "tasks_disabled")},
           "off_at": iso(now), "lines": lines}
    _save(review, out)
    _log(R, f"OFF: {'; '.join(lines) or 'nothing to undo'}")
    _refresh_state_now()
    return out


def summary(st: dict) -> dict:
    """The few figures the hub's line shows: since, tasks paused, Ollama unloaded/stopped."""
    oll = st.get("ollama")
    return {"on": bool(st.get("on")), "since": st.get("since") or "", "paused": len(st.get("paused") or []),
            "unloaded": isinstance(oll, dict) and bool(oll), "stopped": bool((oll or {}).get("stopped")),
            "tasks_disabled": len(st.get("tasks_disabled") or [])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["status", "on", "off"])
    a = ap.parse_args(argv)
    if a.command == "status":
        st = status()
        print(json.dumps(st, indent=2))
        return 0
    st = on() if a.command == "on" else off()
    print("\n".join(st.get("lines") or []) or "done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
