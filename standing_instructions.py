"""Persistent, scoped jobs for the Denisse tray, not Codex scheduled tasks."""
from contextlib import contextmanager
import copy
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import threading
import time
import uuid

import agent_loop
import agent_tools
import runtime_config

ROOT = Path(__file__).resolve().parent
# The code knows how to run these three; what each does, how often and its
# wording are data (runtime_config).
JOBS = {"task_review", "project_review", "downloads"}
log = logging.getLogger("denisse.standing_instructions")


def _cfg():
    return runtime_config.get("standing_instructions")


def _say(key, /, **fields):
    return runtime_config.wording("standing_instructions", key, **fields)


def _repo_path(key):
    return ROOT.joinpath(*_cfg()["files"][key].replace("\\", "/").split("/"))


def defaults():
    """A fresh tray's settings: off, with the jobs from runtime settings
    (runtime.defaults.json; config.json settings.runtime overrides). Jobs
    that fail validate() log a line and the shipped jobs are used."""
    jobs = copy.deepcopy(_cfg()["jobs"])
    data = {"version": 1, "enabled": False, "project_dir": str(ROOT), "jobs": jobs}
    try:
        return validate(data)
    except ValueError as exc:
        log.warning("standing_instructions jobs in config.json settings.runtime: %s -- using the shipped jobs", exc)
        data["jobs"] = copy.deepcopy(runtime_config.defaults()["standing_instructions"]["jobs"])
        return data


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        scratch.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(scratch, path)
    finally:
        scratch.unlink(missing_ok=True)


@contextmanager
def file_lock(path):
    """Nonblocking OS lock: released by the kernel even after a crashed process."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def validate(data):
    if (not isinstance(data, dict) or set(data) != {"version", "enabled", "project_dir", "jobs"}
            or type(data["version"]) is not int or data["version"] != 1
            or type(data["enabled"]) is not bool
            or not isinstance(data["project_dir"], str) or not data["project_dir"].strip()
            or not isinstance(data["jobs"], list) or not 1 <= len(data["jobs"]) <= len(JOBS)):
        raise ValueError(_say("invalid_settings"))
    limits = _cfg()
    least = limits["min_every_seconds"]
    seen = set()
    for job in data["jobs"]:
        if (not isinstance(job, dict) or set(job) != {"id", "enabled", "every_seconds", "goal"}
                or not isinstance(job["id"], str) or job["id"] not in JOBS or job["id"] in seen
                or type(job["enabled"]) is not bool
                or type(job["every_seconds"]) is not int
                or not least.get(job["id"], least["other"]) <= job["every_seconds"] <= limits["max_every_seconds"]
                or not isinstance(job["goal"], str) or not 1 <= len(job["goal"].strip()) <= limits["max_goal_chars"]):
            raise ValueError(_say("invalid_job"))
        seen.add(job["id"])
    return data


class Engine:
    def __init__(self, config=None, state=None, reports=None, model=None):
        self.config = Path(config or _repo_path("config"))
        self.state = Path(state or _repo_path("state"))
        self.reports = Path(reports or _repo_path("reports"))
        self.model = model if model is not None else agent_loop.LocalModel()
        self.stop = threading.Event()
        self.error = ""

    def settings(self):
        if not self.config.exists():
            return defaults()
        if self.config.stat().st_size > _cfg()["max_file_bytes"]:
            raise ValueError(_say("too_large"))
        return validate(json.loads(self.config.read_text(encoding="utf-8-sig")))

    def status(self):
        try:
            config = self.settings()
            state = self._read_state()
            return {**config, "error": self.error, "last_runs": state}
        except (OSError, ValueError) as exc:
            return {**defaults(), "error": str(exc)}

    def _read_state(self):
        state = json.loads(self.state.read_text(encoding="utf-8")) if self.state.exists() else {}
        if (not isinstance(state, dict) or any(key not in JOBS or not isinstance(value, dict)
                or type(value.get("last_started")) not in (int, float)
                or not math.isfinite(value["last_started"]) or value["last_started"] < 0
                for key, value in state.items())):
            raise ValueError(_say("invalid_state"))
        return state

    def set_enabled(self, enabled, job_id=None):
        with file_lock(self.reports / "locks" / "config.lock") as held:
            if not held:
                raise RuntimeError(_say("busy"))
            config = self.settings()
            if job_id is None:
                config["enabled"] = bool(enabled)
            else:
                job = next((j for j in config["jobs"] if j["id"] == job_id), None)
                if job is None:
                    raise ValueError(_say("unknown_job"))
                job["enabled"] = bool(enabled)
            atomic_json(self.config, config)

    def _state_update(self, update):
        with file_lock(self.reports / "locks" / "state.lock") as held:
            if not held:
                return None
            state = self._read_state()
            answer = update(state)
            if answer is not None:
                atomic_json(self.state, state)
            return answer

    def run_job(self, job_id, *, now=None):
        if job_id not in JOBS:
            raise ValueError(_say("unknown_job"))
        with file_lock(self.reports / "locks" / (job_id + ".lock")) as held:
            if not held:
                return None
            claimed = False
            try:
                config = self.settings()
                job = next((j for j in config["jobs"] if j["id"] == job_id), None)
                if self.stop.is_set() or not config["enabled"] or not job or not job["enabled"]:
                    return None
                started = time.time() if now is None else now
                def claim(state):
                    prior = state.get(job_id, {})
                    last = prior.get("last_started")
                    if last is not None and (type(last) not in (int, float) or started < last + job["every_seconds"]):
                        return None
                    state[job_id] = {**prior, "last_started": started, "status": "running"}
                    return dict(prior)
                prior = self._state_update(claim)
                if prior is None:
                    return None
                claimed = True

                def active():
                    current = self.settings()
                    return (not self.stop.is_set() and current["enabled"]
                            and current["project_dir"] == config["project_dir"]
                            and job in current["jobs"])

                if job_id == "downloads":
                    if not active():
                        return None
                    evidence = agent_tools.organize_downloads()
                    result = {"status": "completed", "steps": [{"tool": "organize_downloads", "note": _say("downloads_note")}],
                              "evidence": {"organize_downloads": evidence}, "suggestion": "", "focus_ids": [], "error": ""}
                else:
                    name = "task_snapshot" if job_id == "task_review" else "project_snapshot"
                    tool = (agent_tools.task_snapshot if job_id == "task_review" else
                            lambda: agent_tools.project_snapshot(Path(config["project_dir"])))
                    result = agent_loop.run(job["goal"], {name: tool}, {name}, self.model, active=active)
                result.update(job_id=job_id, started=started, goal=job["goal"])
                # Compare observed data, not model wording or timestamps.
                digest = hashlib.sha256(json.dumps(result["evidence"], sort_keys=True).encode()).hexdigest()
                result["changed"] = (digest != prior.get("digest")) if job_id != "downloads" else bool(result["evidence"]["organize_downloads"]["moved"])
                result["brief"] = agent_tools.render_brief(job_id, result)
                def finish(state):
                    record = state[job_id]
                    record.update(status=result["status"], error=result["error"], brief=result["brief"])
                    if result["status"] == "completed":
                        record["digest"] = digest
                    return True
                if self._state_update(finish) is None:
                    raise RuntimeError(_say("save_failed"))
                if job_id != "downloads" or result["changed"]:
                    atomic_json(self.reports / (job_id + "-latest.json"), result)
                self.error = ""
                return result
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                result = {"job_id": job_id, "status": "failed", "error": self.error,
                          "changed": False, "brief": _say("failed_brief", job_id=job_id, error=self.error)}
                if claimed:
                    def fail(state):
                        state[job_id].update(status="failed", error=self.error, brief=result["brief"])
                        return True
                    try:
                        self._state_update(fail)
                        atomic_json(self.reports / (job_id + "-latest.json"), result)
                    except (OSError, ValueError):
                        pass  # retain the original exception; never replay the action
                # A corrupt state must not be silently replaced or re-run.
                return None if isinstance(exc, json.JSONDecodeError) else result

    def serve(self, jobs, notify=lambda result: None, ready=lambda: True):
        while not self.stop.is_set():
            if not ready():
                self.stop.wait(_cfg()["poll_seconds"])
                continue
            for job_id in jobs:
                result = self.run_job(job_id)
                if (result and result["status"] == "completed" and result["changed"]
                        and self.status()["enabled"] and not self.stop.is_set()):
                    try:
                        notify(result)
                    except Exception:
                        pass  # notification failure must not replay actions
            self.stop.wait(_cfg()["poll_seconds"])
