"""Orchestra runner: one tick = pick the top task in .review/queue/, run it in a
fresh git worktree through a model lane, check the result itself, act on it.

Run by Windows Task Scheduler every 2 minutes (see install_task.ps1), or by hand:
    .venv\\Scripts\\python.exe orchestra\\runner.py            # one tick
    .venv\\Scripts\\python.exe orchestra\\runner.py --dry-run  # show what it would run

What a tick does with a session's result (after Thomas's review, 31 Oct 2026):
  * PASS is not taken on the session's word: the runner re-runs the task's gate
    commands in the worktree and merges only if they exit 0, the hooks loaded,
    and nothing was written into the main checkout
  * HUMAN line, PARTIAL or FAIL -> parked; editing the task (new content) re-queues it
  * timeout / no report / lane missing -> one retry in a fresh worktree, then parked
  * a task whose exact content already PASSed recently is not run again
  * daily spend cap from claude's own total_cost_usd; maintenance <= 20% of runtime
  * every limit lives in orchestra/config.json with its reason
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("ORCHESTRA_REPO", HERE.parent))
REVIEW = REPO / ".review"
QUEUE, REPORTS, ARCHIVE, PARKED = (REVIEW / d for d in ("queue", "reports", "archive", "parked"))
STATE_FILE, LOCK_FILE, LOG_FILE = REVIEW / "state.json", REVIEW / "runner.lock", REVIEW / "runner.log"
PROMPT_FILE = REPO / "orchestra" / "review_prompt.md"  # also usable as .claude/commands/review.md
GUARD = HERE / "hooks" / "guard.py"
WORKTREES = REPO.parent / f"{REPO.name}-wt"
INFRA = ("TIMEOUT", "NO_REPORT", "LANE_MISSING", "WORKTREE_FAILED")
BROKEN_GATE_CODES = {5, 127, 9009}  # pytest: no tests; command not found (sh / cmd)


def load_config() -> dict:
    cfg = {}
    for path in (HERE / "config.json", REPO / "orchestra" / "config.json"):
        if path.exists():
            cfg = {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items()
                   if not k.startswith("_")}
            break
    for k, v in list(cfg.items()):
        env = os.environ.get(f"ORCHESTRA_{k.upper()}")
        if env is not None:
            cfg[k] = json.loads(env) if env[:1] in "{[" else type(v)(env) if not isinstance(v, dict) else v
    return cfg


C = load_config()

# Lanes: argv lists; the prompt goes in on stdin. Override in .review/lanes.json.
# The allow list is broad on purpose; the real limits are in hooks/guard.py, which the
# runner passes with --settings (Thomas: a prefix allow list mostly buys refusals).
DEFAULT_LANES = {
    "claude": ["claude", "-p", "--permission-mode", "acceptEdits", "--permission-prompts", "none",
               "--output-format", "json", "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash",
               "--disallowedTools", "Bash(git push:*),Bash(gh:*)"],
    "codex": ["codex", "exec", "--full-auto", "-"],
}
def npm_bin(pkg: str, name: str) -> str | None:
    """A global npm CLI's JS entry, run with node so long prompts never go through cmd.exe."""
    root = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / pkg
    try:
        meta = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rel = meta["bin"] if isinstance(meta.get("bin"), str) else (meta.get("bin") or {}).get(name)
    return str(root / rel) if rel else None


_gemini = npm_bin("@google/gemini-cli", "gemini")
if _gemini:
    # auto_edit: may edit files, may not run shell commands. The runner runs the gate itself.
    DEFAULT_LANES["gemini"] = ["node", _gemini, "--approval-mode", "auto_edit", "-o", "text",
                               "-p", "Do the task given above."]
VALID = {"priority": ("urgent", "normal"), "weight": ("light", "heavy"), "kind": ("work", "maintenance")}


def now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def log(msg: str) -> None:
    line = f"{now().isoformat()} {msg}"
    print(line)
    REVIEW.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- tasks

@dataclass
class Task:
    path: Path
    title: str
    fields: dict
    gate: str
    digest: str

    @property
    def slug(self) -> str:
        return self.path.stem

    @property
    def priority(self) -> str:
        return self.fields.get("priority", "normal").lower()

    @property
    def weight(self) -> str:
        return self.fields.get("weight", "light").lower()

    @property
    def kind(self) -> str:
        return self.fields.get("kind", "work").lower()

    @property
    def lane(self) -> str:
        return self.fields.get("lane", "claude").lower()

    @property
    def auto(self) -> bool:
        return self.fields.get("queue", "").lower() == "auto"

    @property
    def gate_commands(self) -> list[str]:
        """Commands in fenced code blocks under ## Gate: what the runner itself re-runs."""
        cmds = []
        for block in re.findall(r"```[^\n]*\n(.*?)```", self.gate, re.S):
            cmds += [l.strip() for l in block.splitlines() if l.strip() and not l.strip().startswith("#")]
        return cmds


HEADER = re.compile(r"^([A-Za-z]+):\s*(.*?)\s*$")


def parse_task(path: Path) -> Task:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    title = next((l[2:].strip() for l in lines if l.startswith("# ")), path.stem)
    fields: dict = {}
    for line in lines[1:20]:
        m = HEADER.match(line)
        if m:
            fields.setdefault(m.group(1).lower(), m.group(2))
    m = re.search(r"^##\s*Gate\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    gate = m.group(1).strip() if m else ""
    digest = hashlib.sha1(text.replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()
    return Task(path, title, fields, gate, digest)


def load_queue() -> list[Task]:
    QUEUE.mkdir(parents=True, exist_ok=True)
    return [parse_task(p) for p in sorted(QUEUE.glob("*.md"))]


# ---------------------------------------------------------------- state

def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    for k in ("tasks", "days", "done"):
        st.setdefault(k, {})
    st.setdefault("durations", {"light": [], "heavy": []})
    return st


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def today_usage(state: dict) -> dict:
    u = state["days"].setdefault(now().date().isoformat(), {})
    for k in ("work_s", "maint_s", "cost_usd", "known_cost_s"):
        u.setdefault(k, 0)
    u.setdefault("estimated_runs", 0)
    return u


def maintenance_allowed(state: dict) -> bool:
    u = today_usage(state)
    total = u["work_s"] + u["maint_s"]
    return u["maint_s"] < C["maint_floor_s"] or bool(total and u["maint_s"] / total < C["maint_share"])


def spend_left(state: dict) -> float:
    return C["daily_spend_cap_usd"] - today_usage(state)["cost_usd"]


def timeout_for(weight: str, state: dict) -> int:
    """Starting guess from config; from measured durations once there are enough."""
    runs = state["durations"].get(weight, [])[-200:]
    if len(runs) >= C["timeout_min_runs"]:
        p95 = statistics.quantiles(runs, n=20)[-1]
        return int(max(C["timeout_floor_s"], C["timeout_p95_factor"] * p95))
    return int(C["timeout_s"][weight])


def task_state(state: dict, task: Task) -> dict:
    ts = state["tasks"].setdefault(task.slug, {"attempts": 0, "history": []})
    if ts.get("digest") != task.digest:  # edited (or new) task: a fresh start
        ts.update(digest=task.digest, attempts=0, last_result=None, last_end=None)
    return ts


# ---------------------------------------------------------------- picking

def header_problem(task: Task) -> str | None:
    """Refuse, don't coerce (Thomas, 9 Oct: tasks with a date line first or no Queue line)."""
    first = task.path.read_text(encoding="utf-8").lstrip().splitlines()[:1]
    if not first or not first[0].startswith("# "):
        return "first line must be '# <title>'"
    if "queue" not in task.fields:
        return "no Queue: line"
    for key, allowed in VALID.items():
        if key in task.fields and task.fields[key].lower() not in allowed:
            return f"{key.title()}: {task.fields[key]!r} is not one of {'/'.join(allowed)}"
    if task.lane not in lanes():
        return f"Lane: {task.lane!r} is not a configured lane"
    try:
        dt.datetime.fromisoformat(task.fields.get("written", ""))
    except ValueError:
        return "Written: is missing or not YYYY-MM-DDTHH:MM"
    return None


def why_not_runnable(task: Task, state: dict) -> str | None:
    bad = header_problem(task)
    if bad:
        return f"bad header: {bad}"
    if not task.auto:
        return "Queue is not auto"
    age = now() - dt.datetime.fromisoformat(task.fields["written"])
    if age.days > C["stale_days"]:
        return f"stale: written {age.days} days ago (limit {C['stale_days']}); re-date it if it still holds"
    if not task.gate:
        return "no ## Gate section"
    if task.gate.startswith("<"):
        return "gate is still the template placeholder"
    done = state["done"].get(task.digest)
    if done and now() - dt.datetime.fromisoformat(done) < dt.timedelta(days=C["done_window_days"]):
        return f"this exact task already PASSed on {done}"
    ts = task_state(state, task)
    if ts["attempts"] > C["infra_retries"]:
        return "retry limit reached"
    if ts.get("last_end") and ts.get("last_result") in INFRA:
        if now() < dt.datetime.fromisoformat(ts["last_end"]) + dt.timedelta(minutes=C["backoff_min"]):
            return "waiting before its retry"
    if task.kind == "maintenance" and not maintenance_allowed(state):
        return f"maintenance cap ({int(C['maint_share'] * 100)}%) reached today"
    if spend_left(state) <= 0:
        return f"daily spend cap ${C['daily_spend_cap_usd']:.2f} reached"
    return None


def pick(tasks: list[Task], state: dict) -> Task | None:
    runnable = [t for t in tasks if why_not_runnable(t, state) is None]
    runnable.sort(key=lambda t: (t.priority != "urgent", t.kind == "maintenance", t.slug))
    return runnable[0] if runnable else None


# ---------------------------------------------------------------- lock + watchdog

def take_lock(state: dict) -> bool:
    """Atomic create; a lock older than the longest possible run is stale."""
    max_age = max(timeout_for(w, state) for w in ("light", "heavy")) + C["tick_watchdog_extra_s"]
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "since": now().isoformat()}, f)
            return True
        except FileExistsError:
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age < max_age:
                return False
            log(f"stale lock ({int(age)}s old) removed")
            LOCK_FILE.unlink(missing_ok=True)
    return False


def set_lock_task(slug: str) -> None:
    LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "since": now().isoformat(), "task": slug}),
                         encoding="utf-8")


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def start_watchdog(seconds: int) -> threading.Timer:
    def fire():
        log(f"WATCHDOG: tick still running after {seconds}s; exiting so the next tick can run")
        release_lock()
        os._exit(3)
    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()
    return t


# ---------------------------------------------------------------- git + worktrees

def git(*args: str, cwd: Path = REPO, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=C["git_timeout_s"], check=check)


def is_link(p: Path) -> bool:
    return p.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(p))


def unlink_link(p: Path) -> None:
    """Remove a symlink/junction itself, never what it points at."""
    if is_link(p):
        try:
            os.unlink(p)
        except OSError:
            os.rmdir(p)


def link_venv(wt: Path) -> None:
    """The venv is git-ignored, so a worktree has none; link the main one in so the
    session and the gate can run .venv\\Scripts\\python.exe."""
    src, dst = REPO / ".venv", wt / ".venv"
    if not src.exists() or dst.exists():
        return
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], capture_output=True,
                       timeout=C["git_timeout_s"])
    else:
        os.symlink(src, dst, target_is_directory=True)


def remove_worktree(wt: Path, branch: str, delete_branch: bool) -> None:
    unlink_link(wt / ".venv")  # first, so nothing can recurse into the real venv
    git("worktree", "remove", "--force", str(wt), check=False)
    if delete_branch:
        git("branch", "-D", branch, check=False)


def fresh_worktree(task: Task) -> tuple[Path, str]:
    """Always a clean start: a retry must not build on the failed attempt's mess."""
    branch = f"review/{task.slug}"
    wt = WORKTREES / task.slug
    if wt.exists():
        remove_worktree(wt, branch, delete_branch=False)
    git("worktree", "prune", check=False)
    git("branch", "-D", branch, check=False)
    WORKTREES.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-b", branch, str(wt), "HEAD")
    link_venv(wt)
    return wt, branch


def main_checkout_status() -> set[str]:
    r = git("status", "--porcelain", check=False)
    return {l for l in r.stdout.splitlines() if l.strip() and ".review/" not in l}


def try_merge(branch: str) -> str:
    ahead = git("rev-list", "--count", f"HEAD..{branch}", check=False).stdout.strip()
    if ahead in ("", "0"):
        return "no commits"
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        return "main checkout has uncommitted changes; merge by hand"
    r = git("merge", "--no-ff", "-m", f"orchestra: merge {branch}", branch, check=False)
    if r.returncode != 0:
        git("merge", "--abort", check=False)
        return "merge conflict; merge by hand"
    return "merged"


# ---------------------------------------------------------------- lanes

def lanes() -> dict:
    cfg = dict(DEFAULT_LANES)
    custom = REVIEW / "lanes.json"
    if custom.exists():
        cfg.update(json.loads(custom.read_text(encoding="utf-8")))
    return cfg


def build_prompt(task_rel: str, report_rel: str) -> str:
    template = PROMPT_FILE.read_text(encoding="utf-8")
    template = re.sub(r"\A---.*?---\s*", "", template, flags=re.S)
    return template.replace("$ARGUMENTS", f"{task_rel} {report_rel}")


def hook_settings(wt: Path) -> Path:
    py = sys.executable.replace("pythonw.exe", "python.exe")
    cmd = f'"{py}" "{GUARD}"'
    settings = {"hooks": {"PreToolUse": [{"matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
                                          "hooks": [{"type": "command", "command": cmd, "timeout": 10}]}]}}
    path = wt / ".review" / "settings.json"
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return path


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=60)
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_lane(task: Task, wt: Path, report: Path, timeout: int) -> tuple[str, int]:
    """Run the lane inside the worktree: the task is copied to <wt>/.review/task.md and the
    lane writes <wt>/.review/report.md, which is then copied to `report`.
    Returns (outcome, seconds); outcome is 'ran', 'TIMEOUT' or 'LANE_MISSING: ...'."""
    argv = lanes().get(task.lane)
    if not argv:
        return f"LANE_MISSING: no lane called {task.lane!r}", 0
    exe = shutil.which(argv[0]) or argv[0]
    if not Path(exe).exists() and not shutil.which(exe):
        return f"LANE_MISSING: {argv[0]} not on PATH", 0
    local = wt / ".review"
    local.mkdir(exist_ok=True)
    shutil.copyfile(task.path, local / "task.md")
    local_report = local / "report.md"
    local_report.unlink(missing_ok=True)
    argv = list(argv)
    if task.lane == "claude":
        argv += ["--settings", str(hook_settings(wt))]
    # claude -p started from inside another Claude Code session returns nothing unless these
    # are stripped (Thomas's notes); harmless when they aren't set.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")}
    env.update(ORCHESTRA_TASK=str(local / "task.md"), ORCHESTRA_REPORT=str(local_report),
               ORCHESTRA_WORKTREE=str(wt), ORCHESTRA_MAIN=str(REPO))
    out = (REPORTS / f"{task.slug}.lane.log").open("w", encoding="utf-8")
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    start = time.time()
    proc = subprocess.Popen([exe, *argv[1:]], cwd=wt, env=env, stdin=subprocess.PIPE,
                            stdout=out, stderr=subprocess.STDOUT, text=True, encoding="utf-8", **kw)
    try:
        proc.stdin.write(build_prompt(".review/task.md", ".review/report.md"))
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=timeout)
        outcome = "ran"
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        outcome = "TIMEOUT"
    out.close()
    if local_report.exists():
        shutil.copyfile(local_report, report)
    return outcome, int(time.time() - start)


def run_gate(task: Task, wt: Path) -> tuple[str, str]:
    """The runner's own check before any merge. Returns (verdict, detail);
    verdict is 'pass', 'fail', 'broken' or 'none'."""
    cmds = task.gate_commands
    if not cmds:
        return "none", "the ## Gate has no ``` command block the runner can run"
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, cwd=wt, shell=True, capture_output=True, text=True,
                               timeout=C["gate_timeout_s"], encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return "broken", f"`{cmd}` timed out after {C['gate_timeout_s']}s"
        if r.returncode in BROKEN_GATE_CODES:
            return "broken", f"`{cmd}` exited {r.returncode} (no tests found / command not found)"
        if r.returncode != 0:
            tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
            return "fail", f"`{cmd}` exited {r.returncode}: {' | '.join(tail)}"
    return "pass", f"{len(cmds)} command(s) exited 0"


# ---------------------------------------------------------------- reports

def read_report(report: Path) -> dict:
    if not report.exists():
        return {"result": "NO_REPORT", "human": "none", "note": "no report written", "premise": "not stated"}
    text = report.read_text(encoding="utf-8")
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    m = re.match(r"RESULT:\s*(PASS|PARTIAL|FAIL)\b", first, re.I)
    h = re.search(r"^HUMAN:\s*(.+)$", text, re.M)
    p = re.search(r"^PREMISE:\s*(.+)$", text, re.M)
    return {"result": m.group(1).upper() if m else "FAIL",
            "human": h.group(1).strip() if h else "none",
            "note": "" if m else "first line is not RESULT: PASS|PARTIAL|FAIL",
            "premise": p.group(1).strip() if p else "not stated"}


def lane_result(lane_log: Path) -> dict:
    """claude's --output-format json result: cost and refused tools. A run killed by the
    timeout writes none, so its cost is estimated and its refusals are lost."""
    try:
        text = lane_log.read_text(encoding="utf-8", errors="replace").strip()
        data = json.loads(text if text.startswith("{") else text[text.rfind("\n{") + 1:])
    except (OSError, ValueError):
        return {}
    denied = []
    for d in data.get("permission_denials") or []:
        name = d.get("tool_name", "?")
        cmd = (d.get("tool_input") or {}).get("command")
        denied.append(f"{name}({cmd.split()[0]})" if name == "Bash" and cmd else name)
    return {"cost_usd": data.get("total_cost_usd"), "denied": denied}


def needs_human(human: str) -> bool:
    return human.strip().strip(".").lower() not in ("none", "nothing", "-", "n/a", "")


def move(src: Path, dest_dir: Path, name: str | None = None) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (name or src.name)
    if src.exists():
        os.replace(src, dest)
    return dest


def park(task: Task, reason: str, report: Path | None) -> None:
    move(task.path, PARKED)
    if report and report.exists():
        move(report, PARKED, f"{task.slug}.report.md")
    (PARKED / f"{task.slug}.why.txt").write_text(
        f"{now().isoformat()} {reason}\nTo run it again: edit the task, then move it back to .review/queue/.\n",
        encoding="utf-8")
    log(f"PARKED {task.slug}: {reason}")


def charge(state: dict, task: Task, seconds: int, cost: float | None) -> str:
    u = today_usage(state)
    u["maint_s" if task.kind == "maintenance" else "work_s"] += seconds
    if cost is not None:
        u["cost_usd"] += cost
        u["known_cost_s"] += seconds
        return f"${cost:.2f}"
    if task.lane != "codex" and seconds:  # codex reports no cost; it runs on its own plan
        rate = (u["cost_usd"] / (u["known_cost_s"] / 60)) if u["known_cost_s"] >= 60 else C["unknown_cost_per_min_usd"]
        est = rate * seconds / 60
        u["cost_usd"] += est
        u["estimated_runs"] += 1
        return f"~${est:.2f} (estimated)"
    return "unknown"


# ---------------------------------------------------------------- tick

def finish(task: Task, state: dict, wt: Path, branch: str, outcome: str, seconds: int,
           before_status: set[str] = frozenset()) -> str:
    report = REPORTS / f"{task.slug}.md"
    ts = task_state(state, task)
    lane_info = lane_result(REPORTS / f"{task.slug}.lane.log")
    spent = charge(state, task, seconds, lane_info.get("cost_usd"))

    if outcome == "TIMEOUT":
        info = {"result": "TIMEOUT", "human": "none", "premise": "not stated", "note": f"killed after {seconds}s"}
    else:
        info = read_report(report)
        if info["result"] != "NO_REPORT":
            state["durations"].setdefault(task.weight, []).append(seconds)
    stray = sorted(main_checkout_status() - before_status)
    hooks_loaded = task.lane != "claude" or (wt / ".review" / "hook.log").exists()

    entry = {"end": now().isoformat(), "result": info["result"], "seconds": seconds, "lane": task.lane,
             "weight": task.weight, "note": info["note"], "premise": info["premise"], "spent": spent,
             "denied": lane_info.get("denied", []), "main_checkout_changed": stray,
             "hooks_loaded": hooks_loaded}
    ts["history"].append(entry)
    ts.update(last_end=entry["end"], last_result=info["result"])
    log(f"{info['result']} {task.slug} after {seconds}s (attempt {ts['attempts']}, {spent}) {info['note']}")

    if info["result"] == "PASS":
        problems = []
        if stray:
            problems.append(f"files changed in the main checkout during the run: {', '.join(stray)}")
        if not hooks_loaded:
            problems.append("the guard hook never fired, so its limits weren't enforced")
        verdict, detail = run_gate(task, wt)
        entry["runner_gate"] = f"{verdict}: {detail}"
        log(f"runner gate {task.slug}: {verdict}: {detail}")
        if verdict != "pass":
            problems.append(f"runner's own gate check: {verdict}: {detail}")
        if problems:
            park(task, "session said PASS, but " + "; ".join(problems) + f". Branch {branch} kept.", report)
            return "PASS-REJECTED"
        merge = try_merge(branch)
        ts["merge"] = merge
        log(f"merge {branch}: {merge}")
        state["done"][task.digest] = now().isoformat()
        day = ARCHIVE / now().date().isoformat()
        move(task.path, day)
        move(report, day, f"{task.slug}.report.md")
        if merge in ("merged", "no commits"):
            remove_worktree(wt, branch, delete_branch=True)
        return "PASS"
    if needs_human(info["human"]):
        park(task, f"waiting on Chuen: {info['human']}", report)
    elif info["result"] in ("PARTIAL", "FAIL"):
        park(task, f"{info['result']}: a blind re-run won't change it (see report)", report)
    elif ts["attempts"] > C["infra_retries"]:
        park(task, f"{info['result']} again after {ts['attempts']} attempts", report)
    elif report.exists():
        os.replace(report, REPORTS / f"{task.slug}.attempt{ts['attempts']}.md")
    return info["result"]


def tick(dry_run: bool = False) -> str:
    for d in (QUEUE, REPORTS, ARCHIVE, PARKED):
        d.mkdir(parents=True, exist_ok=True)
    state = load_state()
    tasks = load_queue()
    if dry_run:
        for t in tasks:
            print(f"{t.slug}: {why_not_runnable(t, state) or 'runnable'}"
                  f" [{t.priority}/{t.weight}/{t.kind}/{t.lane}, gate commands: {len(t.gate_commands)}]")
        t = pick(tasks, state)
        print(f"would run: {t.slug if t else 'nothing'}; spend left today ${spend_left(state):.2f}")
        return "dry-run"
    if not take_lock(state):
        return "busy"
    try:
        task = pick(tasks, state)
        if not task:
            save_state(state)
            return "idle"
        timeout = timeout_for(task.weight, state)
        start_watchdog(timeout + C["tick_watchdog_extra_s"])
        ts = task_state(state, task)
        ts["attempts"] += 1
        ts["last_start"] = now().isoformat()
        save_state(state)  # count the attempt before running, so a crash still counts
        set_lock_task(task.slug)
        log(f"START {task.slug} lane={task.lane} weight={task.weight} timeout={timeout}s attempt={ts['attempts']}")
        try:
            wt, branch = fresh_worktree(task)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log(f"worktree failed for {task.slug}: {(getattr(e, 'stderr', '') or str(e)).strip()}")
            ts.update(last_end=now().isoformat(), last_result="WORKTREE_FAILED")
            if ts["attempts"] > C["infra_retries"]:
                park(task, "could not create its worktree", None)
            save_state(state)
            return "WORKTREE_FAILED"
        before = main_checkout_status()
        outcome, seconds = run_lane(task, wt, REPORTS / f"{task.slug}.md", timeout)
        if outcome.startswith("LANE_MISSING"):
            log(f"{task.slug}: {outcome}")
            ts.update(last_end=now().isoformat(), last_result="LANE_MISSING")
            if ts["attempts"] > C["infra_retries"]:
                park(task, outcome, None)
            save_state(state)
            return "LANE_MISSING"
        result = finish(task, state, wt, branch, outcome, seconds, before)
        save_state(state)
        return result
    finally:
        release_lock()
        try:
            import state_now
            state_now.write()
        except Exception as e:  # STATE NOW must never break the runner
            log(f"state_now failed: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="list tasks and what would run; run nothing")
    args = ap.parse_args()
    print(tick(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
