"""Orchestra runner: one tick = pick the top task in .review/queue/, run it in
its own git worktree through a model lane, read the report, act on RESULT.

Run by Windows Task Scheduler every 2 minutes (see install_task.ps1), or by hand:
    .venv\\Scripts\\python.exe orchestra\\runner.py            # one tick
    .venv\\Scripts\\python.exe orchestra\\runner.py --dry-run  # show what it would run

Guard rails (lessons from Thomas's Atrium, 31 Oct 2026):
  * retry cap: a task is dispatched at most MAX_ATTEMPTS times, then parked
  * backoff: a non-PASS task waits BACKOFF_MIN * attempts before its next try
  * HUMAN line that isn't "none" -> parked as "waiting on Chuen", never retried
  * every lane run has a hard timeout and its whole process tree is killed
  * maintenance tasks (Kind: maintenance) get at most ~20% of the day's runtime
  * nothing turns log errors into tasks; only Chuen or a chat writes tasks
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(os.environ.get("ORCHESTRA_REPO", Path(__file__).resolve().parent.parent))
REVIEW = REPO / ".review"
QUEUE, REPORTS, ARCHIVE, PARKED = (REVIEW / d for d in ("queue", "reports", "archive", "parked"))
STATE_FILE, LOCK_FILE, LOG_FILE = REVIEW / "state.json", REVIEW / "runner.lock", REVIEW / "runner.log"
PROMPT_FILE = REPO / "orchestra" / "review_prompt.md"  # also usable as .claude/commands/review.md
WORKTREES = REPO.parent / f"{REPO.name}-wt"

MAX_ATTEMPTS = int(os.environ.get("ORCHESTRA_MAX_ATTEMPTS", 3))
BACKOFF_MIN = float(os.environ.get("ORCHESTRA_BACKOFF_MIN", 30))
TIMEOUT_S = {"light": int(os.environ.get("ORCHESTRA_TIMEOUT_LIGHT", 20 * 60)),
             "heavy": int(os.environ.get("ORCHESTRA_TIMEOUT_HEAVY", 60 * 60))}
MAINT_SHARE = 0.20      # maintenance may use this share of the day's runtime...
MAINT_FLOOR_S = 15 * 60  # ...but always gets this much, so it isn't starved at 00:01
GIT_TIMEOUT_S = 120

STALE_DAYS = int(os.environ.get("ORCHESTRA_STALE_DAYS", 7))  # older tasks assume code that has moved

# Lanes: argv lists; the prompt goes in on stdin. Override in .review/lanes.json.
# claude: --permission-prompts none makes anything outside the allow list fail at once
# instead of waiting for a person who isn't there (Thomas, 6 Sep: headless Writes were
# silently denied without it). --output-format json lets the runner record which tools
# were denied, so the allow list can be widened from evidence, not guesses.
DEFAULT_LANES = {
    "claude": ["claude", "-p", "--permission-mode", "acceptEdits", "--permission-prompts", "none",
               "--output-format", "json",
               "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(git:*),Bash(python:*),"
               "Bash(.venv/Scripts/python.exe:*),Bash(.venv\\Scripts\\python.exe:*),"
               "Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(grep:*),Bash(find:*),Bash(wc:*)",
               "--disallowedTools", "Bash(git push:*),Bash(git push --force:*),Bash(gh:*),Bash(rm -rf:*)"],
    "codex": ["codex", "exec", "--full-auto", "-"],
}
VALID = {"priority": ("urgent", "normal"), "weight": ("light", "heavy"), "kind": ("work", "maintenance")}
RESULTS = ("PASS", "PARTIAL", "FAIL")


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

    @property
    def slug(self) -> str:
        return self.path.stem

    @property
    def priority(self) -> str:
        return self.fields.get("priority", "normal").lower()

    @property
    def weight(self) -> str:
        w = self.fields.get("weight", "light").lower()
        return w if w in TIMEOUT_S else "light"

    @property
    def kind(self) -> str:
        return self.fields.get("kind", "work").lower()

    @property
    def lane(self) -> str:
        return self.fields.get("lane", "claude").lower()

    @property
    def auto(self) -> bool:
        return self.fields.get("queue", "").lower() == "auto"


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
    gate = ""
    m = re.search(r"^##\s*Gate\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    if m:
        gate = m.group(1).strip()
    return Task(path, title, fields, gate)


def load_queue() -> list[Task]:
    QUEUE.mkdir(parents=True, exist_ok=True)
    return [parse_task(p) for p in sorted(QUEUE.glob("*.md"))]


# ---------------------------------------------------------------- state

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"tasks": {}, "days": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def today_usage(state: dict) -> dict:
    return state["days"].setdefault(now().date().isoformat(), {"work_s": 0, "maint_s": 0})


def maintenance_allowed(state: dict) -> bool:
    u = today_usage(state)
    total = u["work_s"] + u["maint_s"]
    return u["maint_s"] < MAINT_FLOOR_S or (total and u["maint_s"] / total < MAINT_SHARE)


def task_state(state: dict, slug: str) -> dict:
    return state["tasks"].setdefault(slug, {"attempts": 0, "history": []})


# ---------------------------------------------------------------- picking

def header_problem(task: Task) -> str | None:
    """Refuse, don't coerce: a header the runner would silently reinterpret runs as
    something the writer didn't ask for (Thomas, 9 Oct: 16 tasks with a date line first,
    ~25 with no Queue line)."""
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
    if age.days > STALE_DAYS:
        return f"stale: written {age.days} days ago (limit {STALE_DAYS}); re-date it if it still holds"
    if not task.gate:
        return "no ## Gate section"
    if task.gate.startswith("<"):
        return "gate is still the template placeholder"
    ts = task_state(state, task.slug)
    if ts["attempts"] >= MAX_ATTEMPTS:
        return "retry cap reached"
    if ts.get("last_end") and ts.get("last_result") != "PASS":
        wait = dt.timedelta(minutes=BACKOFF_MIN * ts["attempts"])
        if now() < dt.datetime.fromisoformat(ts["last_end"]) + wait:
            return "backing off after last attempt"
    if task.kind == "maintenance" and not maintenance_allowed(state):
        return "maintenance cap (20%) reached today"
    return None


def pick(tasks: list[Task], state: dict) -> Task | None:
    runnable = [t for t in tasks if why_not_runnable(t, state) is None]
    runnable.sort(key=lambda t: (t.priority != "urgent", t.kind == "maintenance", t.slug))
    return runnable[0] if runnable else None


# ---------------------------------------------------------------- lock

def take_lock() -> bool:
    """One tick at a time. A lock older than the longest timeout is stale."""
    max_age = max(TIMEOUT_S.values()) + 10 * 60
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < max_age:
            return False
        log(f"stale lock ({int(age)}s old) taken over")
    LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "since": now().isoformat()}), encoding="utf-8")
    return True


def set_lock_task(slug: str) -> None:
    LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "since": now().isoformat(), "task": slug}),
                         encoding="utf-8")


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------- git

def git(*args: str, cwd: Path = REPO, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=GIT_TIMEOUT_S, check=check)


def base_branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def ensure_worktree(task: Task) -> tuple[Path, str]:
    branch = f"review/{task.slug}"
    wt = WORKTREES / task.slug
    if wt.exists():
        return wt, branch  # retry continues the previous attempt's worktree
    WORKTREES.mkdir(parents=True, exist_ok=True)
    exists = git("rev-parse", "--verify", "--quiet", branch, check=False).returncode == 0
    if exists:
        git("worktree", "add", str(wt), branch)
    else:
        git("worktree", "add", "-b", branch, str(wt), "HEAD")
    return wt, branch


def remove_worktree(wt: Path, branch: str, delete_branch: bool) -> None:
    git("worktree", "remove", "--force", str(wt), check=False)
    if delete_branch:
        git("branch", "-D", branch, check=False)


def try_merge(branch: str) -> str:
    """Merge a PASSed branch into the checked-out branch, only if that's safe."""
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
    template = re.sub(r"\A---.*?---\s*", "", template, flags=re.S)  # drop frontmatter
    return template.replace("$ARGUMENTS", f"{task_rel} {report_rel}")


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_lane(task: Task, wt: Path, report: Path) -> tuple[str, int]:
    """Run the lane inside the worktree. The task is copied to <wt>/.review/task.md and
    the lane writes <wt>/.review/report.md, so no lane needs access outside its worktree;
    the report is then copied to `report` in the main checkout.
    Returns (outcome, seconds); outcome is 'ran', 'timeout' or 'lane missing: ...'."""
    argv = lanes().get(task.lane)
    if not argv:
        return f"lane missing: no lane called {task.lane!r}", 0
    exe = shutil.which(argv[0]) or argv[0]
    if not Path(exe).exists() and not shutil.which(exe):
        return f"lane missing: {argv[0]} not on PATH", 0
    local = wt / ".review"
    local.mkdir(exist_ok=True)
    shutil.copyfile(task.path, local / "task.md")
    local_report = local / "report.md"
    local_report.unlink(missing_ok=True)
    # claude -p started from inside another Claude Code session returns nothing unless
    # these are stripped (Thomas's notes); harmless when they aren't set.
    env = {k: v for k, v in os.environ.items()
           if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")}
    env.update(ORCHESTRA_TASK=str(local / "task.md"), ORCHESTRA_REPORT=str(local_report),
               ORCHESTRA_WORKTREE=str(wt))
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
        proc.wait(timeout=TIMEOUT_S[task.weight])
        outcome = "ran"
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.wait(timeout=30)
        outcome = "timeout"
    out.close()
    if local_report.exists():
        shutil.copyfile(local_report, report)
    return outcome, int(time.time() - start)


# ---------------------------------------------------------------- reports

def read_report(report: Path) -> dict:
    if not report.exists():
        return {"result": "FAIL", "human": "none", "note": "no report written", "premise": "not stated"}
    text = report.read_text(encoding="utf-8")
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    m = re.match(r"RESULT:\s*(PASS|PARTIAL|FAIL)\b", first, re.I)
    result = m.group(1).upper() if m else "FAIL"
    h = re.search(r"^HUMAN:\s*(.+)$", text, re.M)
    human = h.group(1).strip() if h else "none"
    p = re.search(r"^PREMISE:\s*(.+)$", text, re.M)
    note = "" if m else "first line is not RESULT: PASS|PARTIAL|FAIL"
    return {"result": result, "human": human, "note": note,
            "premise": p.group(1).strip() if p else "not stated"}


def permission_denials(lane_log: Path) -> list[str]:
    """Tool names the session was refused, from claude's --output-format json result."""
    try:
        text = lane_log.read_text(encoding="utf-8", errors="replace").strip()
        data = json.loads(text[text.rfind("\n{") + 1:] if not text.startswith("{") else text)
    except (OSError, ValueError):
        return []
    out = []
    for d in data.get("permission_denials") or []:
        name = d.get("tool_name", "?")
        cmd = (d.get("tool_input") or {}).get("command")
        out.append(f"{name}({cmd.split()[0]})" if name == "Bash" and cmd else name)
    return out


def main_checkout_status() -> set[str]:
    r = git("status", "--porcelain", check=False)
    return {l for l in r.stdout.splitlines() if l.strip() and ".review/" not in l}


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
    (PARKED / f"{task.slug}.why.txt").write_text(f"{now().isoformat()} {reason}\n", encoding="utf-8")
    log(f"PARKED {task.slug}: {reason}")


# ---------------------------------------------------------------- tick

def finish(task: Task, state: dict, wt: Path, branch: str, outcome: str, seconds: int,
           before_status: set[str] = frozenset()) -> str:
    report = REPORTS / f"{task.slug}.md"
    ts = task_state(state, task.slug)
    u = today_usage(state)
    u["maint_s" if task.kind == "maintenance" else "work_s"] += seconds

    if outcome == "timeout":
        info = {"result": "TIMEOUT", "human": "none", "premise": "not stated",
                "note": f"killed after {TIMEOUT_S[task.weight]}s ({task.weight})"}
    else:
        info = read_report(report)
    denied = permission_denials(REPORTS / f"{task.slug}.lane.log")
    stray = sorted(main_checkout_status() - before_status)
    if stray:
        log(f"WARNING {task.slug}: main checkout changed during the run: {stray}")
    ts.update(last_end=now().isoformat(), last_result=info["result"])
    ts["history"].append({"end": ts["last_end"], "result": info["result"], "seconds": seconds,
                          "lane": task.lane, "note": info["note"], "premise": info["premise"],
                          "denied": denied, "main_checkout_changed": stray})
    log(f"{info['result']} {task.slug} after {seconds}s (attempt {ts['attempts']}) {info['note']}")

    if info["result"] == "PASS":
        merge = try_merge(branch)
        ts["merge"] = merge
        log(f"merge {branch}: {merge}")
        day = ARCHIVE / now().date().isoformat()
        move(task.path, day)
        move(report, day, f"{task.slug}.report.md")
        if merge in ("merged", "no commits"):
            remove_worktree(wt, branch, delete_branch=True)
        return "PASS"
    if needs_human(info["human"]):
        park(task, f"waiting on Chuen: {info['human']}", report)
        return info["result"]
    if ts["attempts"] >= MAX_ATTEMPTS:
        park(task, f"retry cap: {MAX_ATTEMPTS} attempts, last {info['result']}", report)
        return info["result"]
    if report.exists():  # keep each attempt's report visible, free the name for the next one
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
                  f" [{t.priority}/{t.weight}/{t.kind}/{t.lane}]")
        t = pick(tasks, state)
        print(f"would run: {t.slug if t else 'nothing'}")
        return "dry-run"
    if not take_lock():
        return "busy"
    try:
        # park anything that hit the cap so it stops showing as open
        for t in tasks:
            if task_state(state, t.slug)["attempts"] >= MAX_ATTEMPTS:
                park(t, "retry cap reached", None)
        task = pick([t for t in tasks if t.path.exists()], state)
        if not task:
            save_state(state)
            return "idle"
        ts = task_state(state, task.slug)
        ts["attempts"] += 1
        ts["last_start"] = now().isoformat()
        save_state(state)  # count the attempt before running, so a crash still counts
        set_lock_task(task.slug)
        log(f"START {task.slug} lane={task.lane} weight={task.weight} attempt={ts['attempts']}")
        try:
            wt, branch = ensure_worktree(task)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            err = getattr(e, "stderr", "") or str(e)
            log(f"worktree failed for {task.slug}: {err.strip()}")
            ts.update(last_end=now().isoformat(), last_result="FAIL")
            save_state(state)
            return "FAIL"
        before = main_checkout_status()
        outcome, seconds = run_lane(task, wt, REPORTS / f"{task.slug}.md")
        if outcome.startswith("lane missing"):
            log(f"{task.slug}: {outcome}")
            ts.update(last_end=now().isoformat(), last_result="FAIL")
            save_state(state)
            return "FAIL"
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
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
