"""Persistent, scoped jobs for the Denisse tray, not Codex scheduled tasks."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

import agent_loop
import agent_tools

ROOT = Path(__file__).resolve().parent
JOBS = {"task_review", "project_review", "downloads"}


def defaults():
    return {"version": 1, "enabled": False, "project_dir": str(ROOT), "jobs": [
        {"id": "task_review", "enabled": True, "every_seconds": 21600,
         "goal": "Review open tasks and their deadlines. Suggest the next useful task; do not complete or create tasks."},
        {"id": "project_review", "enabled": True, "every_seconds": 3600,
         "goal": "Check the project's local Git status. Suggest one next engineering step from the evidence; do not change code or commit."},
        {"id": "downloads", "enabled": True, "every_seconds": 15,
         "goal": "Sort quiet loose Downloads files using the existing type rules; preserve subfolders and collisions."}]}


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
            or not isinstance(data["jobs"], list) or not 1 <= len(data["jobs"]) <= 3):
        raise ValueError("Invalid standing-instruction settings")
    seen = set()
    for job in data["jobs"]:
        if (not isinstance(job, dict) or set(job) != {"id", "enabled", "every_seconds", "goal"}
                or not isinstance(job["id"], str) or job["id"] not in JOBS or job["id"] in seen
                or type(job["enabled"]) is not bool
                or type(job["every_seconds"]) is not int
                or not (15 if job["id"] == "downloads" else 300) <= job["every_seconds"] <= 86400
                or not isinstance(job["goal"], str) or not 1 <= len(job["goal"].strip()) <= 600):
            raise ValueError("Invalid or duplicate standing instruction")
        seen.add(job["id"])
    return data


class Engine:
    def __init__(self, config=None, state=None, reports=None, model=None):
        self.config = Path(config or ROOT / "memory" / "standing_instructions.json")
        self.state = Path(state or ROOT / "memory" / "agent_state.json")
        self.reports = Path(reports or ROOT / "logs" / "agent_runs")
        self.model = model if model is not None else agent_loop.LocalModel()
        self.stop = threading.Event()
        self.error = ""

    def settings(self):
        if not self.config.exists():
            return defaults()
        if self.config.stat().st_size > 16384:
            raise ValueError("Standing instructions are too large")
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
            raise ValueError("Invalid agent state; inspect before resetting")
        return state

    def set_enabled(self, enabled, job_id=None):
        with file_lock(self.reports / "locks" / "config.lock") as held:
            if not held:
                raise RuntimeError("Instructions are being edited; try again")
            config = self.settings()
            if job_id is None:
                config["enabled"] = bool(enabled)
            else:
                job = next((j for j in config["jobs"] if j["id"] == job_id), None)
                if job is None:
                    raise ValueError("Unknown instruction")
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
            raise ValueError("Unknown instruction")
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
                    result = {"status": "completed", "steps": [{"tool": "organize_downloads", "note": "Existing type rules"}],
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
                    raise RuntimeError("Could not save run outcome")
                if job_id != "downloads" or result["changed"]:
                    atomic_json(self.reports / (job_id + "-latest.json"), result)
                self.error = ""
                return result
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                result = {"job_id": job_id, "status": "failed", "error": self.error,
                          "changed": False, "brief": f"{job_id}: {self.error}"}
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
                self.stop.wait(5)
                continue
            for job_id in jobs:
                result = self.run_job(job_id)
                if (result and result["status"] == "completed" and result["changed"]
                        and self.status()["enabled"] and not self.stop.is_set()):
                    try:
                        notify(result)
                    except Exception:
                        pass  # notification failure must not replay actions
            self.stop.wait(5)
