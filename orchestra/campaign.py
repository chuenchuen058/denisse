"""Campaign controls: a hard deadline and pause / resume / stop that the runner enforces
itself, so no chat has to remember to edit config.json at the right moment (Chuen, 2 Nov 2026).

    .venv\\Scripts\\python.exe orchestra\\campaign.py status
    .venv\\Scripts\\python.exe orchestra\\campaign.py pause            # no new dispatch; running tasks finish
    .venv\\Scripts\\python.exe orchestra\\campaign.py resume           # only before the campaign's stop_at
    .venv\\Scripts\\python.exe orchestra\\campaign.py resume --after-deadline   # explicit: dispatch on config.json values
    .venv\\Scripts\\python.exe orchestra\\campaign.py stop             # pause + end the campaign (its overrides stop)
    .venv\\Scripts\\python.exe orchestra\\campaign.py emergency-stop   # stop + kill the runner-owned lane trees
    .venv\\Scripts\\python.exe orchestra\\campaign.py disable-schedule # every tick dispatches nothing until resume

Two files, both in the private runtime area .review/ (git-ignored; the repo is public):

  .review/campaign.json  what Chuen set: deadline + overrides. Validated on every load; anything
                         wrong with it -> the whole file is ignored and logged, never half applied.
  .review/control.json   manual pause / stop / schedule-off. Written only by this script; nothing
                         the runner does (new tick, watchdog, logon trigger) clears it -- only `resume`.

Workers can't change either: hooks/guard.py denies writes to them and running this script's
mutating commands. Timestamps are UTC in the files; Europe/London is for display only.
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
SCHEMA = 1
CAMPAIGN_NAME, CONTROL_NAME = "campaign.json", "control.json"
SEED_NAME = "campaign_seed.json"  # tracked copy of a campaign Chuen set in chat; installed once
OVERRIDE_KEYS = {"lane_slots", "daily_spend_cap_usd"}
FIELDS = {"schema", "id", "purpose", "enabled", "started_at", "stop_at", "stop_at_source",
          "timezone", "overrides", "restart_after_stop"}

PAUSED, ENDED, SCHEDULE_OFF = "paused by Chuen", "campaign ended", "scheduled dispatch disabled by Chuen"


def utcnow() -> dt.datetime:
    """The one clock everything here reads; tests replace it."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def london(t: dt.datetime) -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        return t.astimezone(ZoneInfo("Europe/London"))
    except Exception:  # no tz database: show UTC rather than crash
        return t.astimezone(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(s) -> dt.datetime:
    """An ISO timestamp WITH an offset (Z or +hh:mm). A naive one is refused: its zone is a guess."""
    if not isinstance(s, str):
        raise ValueError(f"{s!r} is not a timestamp string")
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError(f"{s!r} has no UTC offset")
    return t.astimezone(dt.timezone.utc)


# ---------------------------------------------------------------- campaign.json

def validate(data) -> list[str]:
    """Every problem with a campaign, so a bad file is rejected whole."""
    if not isinstance(data, dict):
        return ["top level is not an object"]
    errs = []
    keys = {k for k in data if not str(k).startswith("_")}  # "_note" keys are comments
    if keys - FIELDS:
        errs.append(f"unknown field(s) {sorted(keys - FIELDS)}")
    if FIELDS - keys:
        errs.append(f"missing field(s) {sorted(FIELDS - keys)}")
    if data.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA}, got {data.get('schema')!r}")
    for k in ("id", "purpose", "stop_at_source"):
        if k in data and not (isinstance(data[k], str) and data[k].strip()):
            errs.append(f"{k} must be a non-empty string")
    for k in ("enabled", "restart_after_stop"):
        if k in data and not isinstance(data[k], bool):
            errs.append(f"{k} must be true or false")
    times = {}
    for k in ("started_at", "stop_at"):
        if k in data:
            try:
                times[k] = parse_utc(data[k])
            except ValueError as e:
                errs.append(f"{k}: {e}")
    if len(times) == 2 and times["stop_at"] <= times["started_at"]:
        errs.append("stop_at is not after started_at")
    if "timezone" in data and data["timezone"] != "Europe/London":
        errs.append("timezone must be Europe/London")
    ov = data.get("overrides")
    if "overrides" in data:
        if not isinstance(ov, dict):
            errs.append("overrides must be an object")
        else:
            if set(ov) - OVERRIDE_KEYS:
                errs.append(f"overrides may only set {sorted(OVERRIDE_KEYS)}, not {sorted(set(ov) - OVERRIDE_KEYS)}")
            if "lane_slots" in ov:
                ls = ov["lane_slots"]
                if not (isinstance(ls, dict) and ls and all(
                        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 8
                        for k, v in ls.items())):
                    errs.append("overrides.lane_slots must map lane names to whole numbers 1-8")
            if "daily_spend_cap_usd" in ov:
                cap = ov["daily_spend_cap_usd"]
                if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not 0 <= cap <= 10000:
                    errs.append("overrides.daily_spend_cap_usd must be a number from 0 to 10000")
    return errs


def load_campaign(review: Path, log=None) -> dict | None:
    """The validated campaign, or None (no file, or a bad one -- which is logged, never applied)."""
    path = review / CAMPAIGN_NAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        errs = validate(data)
    except (OSError, ValueError) as e:
        errs = [f"unreadable: {e}"]
    if errs:
        if log:
            log(f"campaign: {path.name} ignored (nothing in it applied): {'; '.join(errs)}")
        return None
    return data


def seed(review: Path, repo: Path, log=None) -> bool:
    """Install the tracked seed as .review/campaign.json once: only if there is no campaign
    file and this seed id was never installed before, so deleting the file sticks."""
    src, dst = repo / "orchestra" / SEED_NAME, review / CAMPAIGN_NAME
    if dst.exists() or not src.exists():
        return False
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if validate(data):
        return False
    ctl = load_control(review)
    if data["id"] in ctl.get("seeded", []):
        return False
    review.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, dst)
    ctl.setdefault("seeded", []).append(data["id"])
    save_control(review, ctl)
    if log:
        log(f"campaign: installed {data['id']} from orchestra/{SEED_NAME}")
    return True


def overrides_active(camp: dict | None, ctl: dict, now: dt.datetime) -> bool:
    """Overrides win only while enabled, inside [started_at, stop_at), and not ended by `stop`."""
    if not camp or not camp["enabled"] or camp["id"] in ctl.get("ended", {}):
        return False
    return parse_utc(camp["started_at"]) <= now < parse_utc(camp["stop_at"])


def apply(cfg: dict, review: Path, now: dt.datetime | None = None, log=None) -> dict:
    """config.json values with the campaign's overrides on top while it is active.
    After stop_at (or disabled, or stopped) config.json applies again, with no edit."""
    out = dict(cfg)
    try:
        camp = load_campaign(review, log)
        if overrides_active(camp, load_control(review), now or utcnow()):
            ov = camp["overrides"]
            if "lane_slots" in ov:
                out["lane_slots"] = {**cfg.get("lane_slots", {}), **ov["lane_slots"]}
            if "daily_spend_cap_usd" in ov:
                out["daily_spend_cap_usd"] = float(ov["daily_spend_cap_usd"])
    except Exception as e:  # a campaign problem must never crash a tick
        if log:
            log(f"campaign: ignored after an error: {e!r}")
        return dict(cfg)
    return out


# ---------------------------------------------------------------- control.json

def load_control(review: Path) -> dict:
    try:
        ctl = json.loads((review / CONTROL_NAME).read_text(encoding="utf-8"))
        return ctl if isinstance(ctl, dict) else {}
    except (OSError, ValueError):
        return {}


def save_control(review: Path, ctl: dict) -> None:
    review.mkdir(parents=True, exist_ok=True)
    ctl["schema"] = SCHEMA
    path = review / CONTROL_NAME
    tmp = path.with_suffix(".ctl.tmp")
    tmp.write_text(json.dumps(ctl, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def dispatch_block(review: Path) -> str | None:
    """Why the runner may start nothing right now, or None. Read before every dispatch.
    A control.json that can't be read blocks nothing -- but one that says paused always does."""
    ctl = load_control(review)
    if ctl.get("schedule_disabled"):
        return SCHEDULE_OFF
    if ctl.get("paused"):
        return ENDED if ctl.get("stopped") else PAUSED
    return None


def _event(ctl: dict, what: str, now: dt.datetime) -> None:
    ctl.setdefault("history", []).append({"at": iso(now), "what": what})
    ctl["history"] = ctl["history"][-50:]


def pause(review: Path, now: dt.datetime | None = None) -> str:
    now = now or utcnow()
    ctl = load_control(review)
    if not ctl.get("paused"):
        ctl.update(paused=True, paused_at=iso(now))
    _event(ctl, "pause", now)
    save_control(review, ctl)
    return "paused: no new tasks start; running ones finish. `campaign.py resume` undoes it."


def stop(review: Path, now: dt.datetime | None = None, what: str = "stop") -> str:
    now = now or utcnow()
    ctl = load_control(review)
    if not ctl.get("paused"):
        ctl.update(paused=True, paused_at=iso(now))
    ctl["stopped"] = True
    camp = load_campaign(review)
    msg = "stopped: no new tasks start."
    if camp:
        ctl.setdefault("ended", {})[camp["id"]] = iso(now)
        msg += f" Campaign {camp['id']} ended; config.json values apply from here."
    _event(ctl, what, now)
    save_control(review, ctl)
    return msg


def resume(review: Path, now: dt.datetime | None = None, after_deadline: bool = False) -> tuple[bool, str]:
    """Only Chuen's explicit action clears a pause. Refused once the campaign's deadline has
    passed unless --after-deadline says, explicitly, to dispatch on config.json's values."""
    now = now or utcnow()
    camp = load_campaign(review)
    if camp and camp["enabled"] and now >= parse_utc(camp["stop_at"]) and not after_deadline:
        return False, (f"refused: campaign {camp['id']} ended at {fmt_london(parse_utc(camp['stop_at']))}. "
                       "`resume --after-deadline` dispatches again on config.json's normal values.")
    ctl = load_control(review)
    for k in ("paused", "paused_at", "stopped", "schedule_disabled"):
        ctl.pop(k, None)
    msg = "resumed: the runner dispatches again."
    if camp and camp["id"] in ctl.get("ended", {}):
        if camp.get("restart_after_stop") and now < parse_utc(camp["stop_at"]):
            ctl["ended"].pop(camp["id"])
            msg += f" Campaign {camp['id']} restarted (restart_after_stop is true)."
        else:
            msg += f" Campaign {camp['id']} stays ended (restart_after_stop is false): config.json values."
    _event(ctl, "resume" + (" --after-deadline" if after_deadline else ""), now)
    save_control(review, ctl)
    return True, msg


def disable_schedule(review: Path, now: dt.datetime | None = None) -> str:
    now = now or utcnow()
    ctl = load_control(review)
    ctl["schedule_disabled"] = True
    _event(ctl, "disable-schedule", now)
    save_control(review, ctl)
    return ("scheduled dispatch disabled: every tick (Task Scheduler, logon, by hand) starts nothing "
            "until `campaign.py resume`. The scheduled task itself still fires and exits at once.")


# ---------------------------------------------------------------- emergency stop

def proc_alive(pid, created=None) -> bool:
    """The runner's own liveness rule: the pid exists AND, when the lock recorded a creation
    time, it is that very process (a reused pid is not the old worker). Tests replace it."""
    sys.path.insert(0, str(HERE))
    import runner
    return runner._alive(pid, created)


def kill_tree_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=60)
    else:
        import signal
        try:
            os.killpg(int(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


LOCK_FIELDS = ("pid", "created", "lane_pid", "lane_created", "lane_job", "task", "since")


def owned_runs(review: Path) -> list[dict]:
    """Runs the runner owns, from its lock files: runner pid (+ creation time), lane pid
    (+ creation time, job name), task slug."""
    out = []
    for lf in sorted(review.glob("runner*.lock")):
        try:
            lk = json.loads(lf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(lk, dict):
            out.append({"lock": lf, **{k: lk.get(k) for k in LOCK_FIELDS}})
    return out


def emergency_stop(R, now: dt.datetime | None = None) -> list[str]:
    """stop, then kill only process trees named in the runner's own lock files (the lane
    process it started and the tick that waits on it), and park those tasks as cancelled.
    Branches and worktrees are kept. `R` is the runner module (paths, park, state).

    Each pid is checked against the creation time the lock recorded, so a reused number is
    never killed. The lane tree goes first (taskkill /T, for a run whose job object could not
    be made); then the tick, which closes its kill-on-close Job Object and takes every
    grandchild of the lane with it (orchestra/README.md, "Process supervision")."""
    now = now or utcnow()
    review = R.REVIEW
    lines = [stop(review, now, what="emergency-stop")]
    for run in owned_runs(review):
        runner_pid, lane_pid, slug = run["pid"], run["lane_pid"], run["task"]
        runner_up = (bool(runner_pid) and runner_pid != os.getpid()
                     and proc_alive(runner_pid, run["created"]))
        # The lane pid is only trusted while its runner is alive and waiting on it: once the
        # runner is gone the number may already belong to an unrelated process.
        if lane_pid and runner_up and proc_alive(lane_pid, run["lane_created"]):
            kill_tree_pid(lane_pid)
            lines.append(f"killed lane process tree {lane_pid} ({slug})")
        if runner_up:
            kill_tree_pid(runner_pid)
            lines.append(f"killed runner tick {runner_pid} ({slug})")
        run["lock"].unlink(missing_ok=True)
        if slug:
            task_path = R.QUEUE / f"{slug}.md"
            if task_path.exists():
                task = R.parse_task(task_path)
                R._BASE = json.loads(json.dumps(R.load_state()))  # merge with lanes saving meanwhile
                state = R.load_state()
                ts = R.task_state(state, task)
                ts.update(last_end=R.now().isoformat(), last_result="CANCELLED")
                ts.setdefault("history", []).append({"end": R.now().isoformat(), "result": "CANCELLED",
                                                     "note": "cancelled by Chuen (emergency-stop)"})
                R.save_state(state)
                R.park(task, f"cancelled by Chuen (emergency-stop); branch review/{slug} and its worktree kept",
                       R.REPORTS / f"{slug}.md")
                lines.append(f"parked {slug} as cancelled by Chuen; branch review/{slug} kept")
    return lines


# ---------------------------------------------------------------- display

def fmt_london(t: dt.datetime) -> str:
    return london(t).strftime("%a %d %b %H:%M %Z")


def fmt_left(delta: dt.timedelta) -> str:
    mins = max(0, int(delta.total_seconds() // 60))
    d, rem = divmod(mins, 24 * 60)
    h, m = divmod(rem, 60)
    return f"{d} d {h} h" if d else f"{h} h {m} min"


def campaign_line(review: Path, now: dt.datetime | None = None) -> str | None:
    """One line for STATE NOW, e.g. 'Sprint: ends Wed 18:00, 1 d 22 h left, Claude ×3'."""
    now = now or utcnow()
    camp = load_campaign(review)
    ctl = load_control(review)
    block = dispatch_block(review)
    tail = f" — dispatch {block}" if block else ""
    if not camp:
        return f"**Dispatch:** {block}" if block else None
    stop_at = parse_utc(camp["stop_at"])
    when = london(stop_at).strftime("%a %H:%M")
    if overrides_active(camp, ctl, now):
        slots = ", ".join(f"{lane.title()} ×{n}" for lane, n in camp["overrides"].get("lane_slots", {}).items())
        cap = camp["overrides"].get("daily_spend_cap_usd")
        extra = f", ${cap:g}/day" if cap is not None else ""
        return f"**Sprint:** ends {when}, {fmt_left(stop_at - now)} left, {slots}{extra}{tail}"
    if camp["id"] in ctl.get("ended", {}):
        return f"**Sprint:** {camp['id']} stopped by Chuen; config.json values apply{tail}"
    if not camp["enabled"]:
        return f"**Sprint:** {camp['id']} disabled; config.json values apply{tail}"
    if now >= stop_at:
        return f"**Sprint:** {camp['id']} ended {when}; config.json values apply{tail}"
    return f"**Sprint:** {camp['id']} starts {london(parse_utc(camp['started_at'])).strftime('%a %H:%M')}{tail}"


def status_text(R, now: dt.datetime | None = None) -> str:
    now = now or utcnow()
    review = R.REVIEW
    camp = load_campaign(review, log=lambda m: None)
    ctl = load_control(review)
    cfg = R.load_config(now_utc=now)
    out = [f"Orchestra campaign status, {fmt_london(now)}"]
    if (review / CAMPAIGN_NAME).exists() and not camp:
        errs = "; ".join(validate(_read_json(review / CAMPAIGN_NAME)) or ["unreadable JSON"])
        out.append(f"Campaign: .review/{CAMPAIGN_NAME} is INVALID and ignored: {errs}")
    elif not camp:
        out.append("Campaign: none (config.json values apply)")
    else:
        stop_at = parse_utc(camp["stop_at"])
        active = overrides_active(camp, ctl, now)
        state = ("active" if active else "stopped by Chuen" if camp["id"] in ctl.get("ended", {})
                 else "disabled" if not camp["enabled"] else "ended" if now >= stop_at else "not started")
        out += [f"Campaign: {camp['id']} ({camp['purpose']}) -- {state}",
                f"  stop at {fmt_london(stop_at)} (source: {camp['stop_at_source']})"
                + (f", {fmt_left(stop_at - now)} left" if now < stop_at else ""),
                f"  overrides {json.dumps(camp['overrides'])}" + (" (in force)" if active else " (not in force)")]
    block = dispatch_block(review)
    out.append(f"Dispatch: {block + ' (only `campaign.py resume` clears it)' if block else 'on'}")
    out.append(f"Spend cap today: ${float(cfg['daily_spend_cap_usd']):.2f}")
    runs = {(r["lock"].name): r for r in owned_runs(review)}
    out.append("Slots:")
    for lane, n in sorted(cfg.get("lane_slots", {}).items()):
        for slot in range(1, int(n) + 1):
            name = f"runner.{lane}.lock" if slot == 1 else f"runner.{lane}.{slot}.lock"
            r = runs.get(name)
            what = f"{r['task'] or '(starting)'} since {r['since'] or '?'}" if r else "free"
            out.append(f"  {lane} {slot}: {what}")
    return "\n".join(out)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["status", "pause", "resume", "stop", "emergency-stop", "disable-schedule"])
    ap.add_argument("--after-deadline", action="store_true",
                    help="resume only: dispatch again after the campaign's deadline, on config.json values")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(HERE))
    import runner as R
    code = 0
    if a.command == "status":
        print(status_text(R))
        return 0
    if a.command == "pause":
        msg = pause(R.REVIEW)
    elif a.command == "stop":
        msg = stop(R.REVIEW)
    elif a.command == "disable-schedule":
        msg = disable_schedule(R.REVIEW)
    elif a.command == "resume":
        ok, msg = resume(R.REVIEW, after_deadline=a.after_deadline)
        code = 0 if ok else 2
    else:
        msg = "\n".join(emergency_stop(R))
    R.log(f"campaign.py {a.command}: {msg.splitlines()[0]}")
    print(msg)
    try:
        import state_now
        state_now.write()
    except Exception as e:  # STATE NOW is a view; never fail the control action over it
        print(f"(STATE NOW not refreshed: {e})")
    return code


if __name__ == "__main__":
    sys.exit(main())
