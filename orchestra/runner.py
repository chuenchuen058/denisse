"""Orchestra runner: one tick = pick the top task in .review/queue/, run it in a
fresh git worktree through a model lane, check the result itself, act on it.

Run by Windows Task Scheduler every 2 minutes (see install_task.ps1), or by hand:
    .venv\\Scripts\\python.exe orchestra\\runner.py            # one tick
    .venv\\Scripts\\python.exe orchestra\\runner.py --dry-run  # show what it would run

What a tick does with a session's result (after Thomas's review, 31 Oct 2026):
  * PASS is not taken on the session's word: the runner re-runs the task's gate
    commands in the worktree and merges only if they exit 0, the hooks loaded,
    and nothing was written into the main checkout
  * a risky diff (orchestra/, atrium/hook.py, git hooks, install_*.ps1, >400 lines) first
    goes to a fresh read-only reviewer lane; VERDICT: block parks it (2 Nov 2026)
  * the branch is then merged onto *current* master in an integration worktree, the gate
    (plus run_checks --fast if it exists) re-runs there, and master is fast-forwarded to
    that exact tested commit; a red combined tree parks it, master moving twice parks it
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

# A lane's usage limit is "come back later", not a failed attempt (2 Nov 2026: Codex printed
# the line below, the runner logged NO_REPORT, spent the retry and parked the task).
# One table: lane -> patterns, checked against the tail of <slug>.lane.log after a run with
# no report. A lane not in the table (e.g. a custom wrapper in lanes.json) is checked
# against every pattern.
CODEX_LIMIT_FIXTURE = ("ERROR: You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
                       "visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 10:55 PM.")
LIMIT_PATTERNS = {
    "codex": [r"you've hit your usage limit", r"usage limit", r"rate limit(ed)? (reached|exceeded)",
              r"429 too many requests"],
    "claude": [r"usage limit reached", r"you've hit your (usage )?limit", r"\b\d+-hour limit reached",
               r"weekly limit reached", r"rate limit (reached|exceeded)", r"rate_limit_error"],
    "gemini": [r"IneligibleTierError", r"quota exceeded", r"exceeded your current quota",
               r"RESOURCE_EXHAUSTED", r"rate limit (reached|exceeded)"],
}
LIMIT_TAIL_LINES = 40
LIMIT_DEFAULT_MIN = 60  # when the message names no time
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
               # stream-json: the lane log fills as the session works, so orchestra/watch.py can
               # show it live (Chuen, 2 Nov); the last line is still the result JSON lane_result reads.
               "--output-format", "stream-json", "--verbose",
               "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash",
               "--disallowedTools", "Bash(git push:*),Bash(gh:*)"],
    # Codex 0.162 dropped --full-auto: workspace-write = edit inside the worktree and
    # run commands there, no network (found 2 Nov 2026, the first Orchestra task on Codex).
    # MCP servers off: with approval "never" Chuen's Desktop Commander etc. fail and the run
    # dies. Full access because Codex's Windows sandbox can't start a shell when launched in
    # the background ("setup refresh had errors"), so workspace-write could only read.
    # Chuen chose this (2 Nov 2026). What still holds: the worktree, the runner's own gate
    # re-run, PASS-only merge, the main-checkout dirty check, and .git/hooks/pre-push,
    # which refuses any push while ORCHESTRA_WORKTREE is set.
    "codex": ["codex", "exec", "--skip-git-repo-check", "--color", "never",
              "-c", "mcp_servers.node_repl.enabled=false",
              "-c", "mcp_servers.desktop-commander.enabled=false",
              "-c", "mcp_servers.obsidian.enabled=false",
              "-s", "danger-full-access", "-"],
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
# The scheduled task's PATH has no %APPDATA%\npm, so give codex its full path (2 Nov 2026).
# The prompt goes in on stdin, so running the .cmd shim is safe for long prompts.
# Always prefer the npm shim: under Task Scheduler `which("codex")` can find a Windows app
# alias that then fails to run. Try the home folder too in case APPDATA is unset.
# npm installs made from inside the Claude desktop app land in its packaged (virtualised)
# AppData, which only the app sees; outside it the real files are under
# %LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming (found 2 Nov 2026).
_local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
if True:
    for _codex_cmd in (Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "npm" / "codex.cmd",
                       *sorted(_local.glob("Packages/Claude_*/LocalCache/Roaming/npm/codex.cmd"))):
        if _codex_cmd.exists():
            DEFAULT_LANES["codex"][0] = str(_codex_cmd)
            break
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


HEADER = re.compile(r"^([A-Za-z][A-Za-z-]*):\s*(.*?)\s*$")  # hyphen: Lane-Fixed:


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
    for k in ("tasks", "days", "done", "lanes"):
        st.setdefault(k, {})
    st.setdefault("durations", {"light": [], "heavy": []})
    return st


_BASE: dict | None = None  # state as this process loaded it; set by tick() when lanes run in parallel


class _FileLock:
    """Spin on an O_EXCL file; a lock older than `stale` seconds is taken over."""
    def __init__(self, path: Path, wait: float = 300, stale: float = 900):
        self.path, self.wait, self.stale = path, wait, stale

    def __enter__(self):
        end = time.time() + self.wait
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale:
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > end:
                    raise TimeoutError(f"{self.path.name} held too long")
                time.sleep(0.5)

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def _merge_into(fresh: dict, state: dict, base: dict) -> dict:
    """Apply what this process changed (state vs base) onto the state another runner saved."""
    for slug, ts in state["tasks"].items():
        if base["tasks"].get(slug) != ts:
            fresh["tasks"][slug] = ts
    for k, v in state["done"].items():
        if base["done"].get(k) != v:
            fresh["done"][k] = v
    for k, v in state.get("lanes", {}).items():
        if base.get("lanes", {}).get(k) != v:
            fresh.setdefault("lanes", {})[k] = v
    for w, runs in state["durations"].items():
        fresh["durations"].setdefault(w, []).extend(runs[len(base["durations"].get(w, [])):])
    for day, u in state["days"].items():
        fu, bu = fresh["days"].setdefault(day, {}), base["days"].get(day, {})
        for k, v in u.items():
            if isinstance(v, (int, float)):
                fu[k] = fu.get(k, 0) + v - bu.get(k, 0)
            elif bu.get(k) != v:
                fu[k] = v
    return fresh


def save_state(state: dict) -> None:
    global _BASE
    if _BASE is not None:  # another lane's runner may have saved since we loaded
        with _FileLock(REVIEW / "state.lock", wait=60, stale=120):
            out = _merge_into(load_state(), state, _BASE)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
            os.replace(tmp, STATE_FILE)
            _BASE = json.loads(json.dumps(state))
        return
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


# ---------------------------------------------------------------- lane limits

def _limit_text(lane_log: Path) -> str:
    """The tail of a lane log as plain text. In claude's stream-json only result/error events
    count, so a session that merely *read* about limits (a tool result) doesn't trip it."""
    try:
        lines = lane_log.read_text(encoding="utf-8", errors="replace").splitlines()[-LIMIT_TAIL_LINES:]
    except OSError:
        return ""
    out = []
    for line in lines:
        s = line.strip()
        if s.startswith("{"):
            try:
                data = json.loads(s)
            except ValueError:
                out.append(s)
                continue
            if isinstance(data, dict) and data.get("type") in ("result", "error"):
                out.append(str(data.get("result") or data.get("error") or data.get("message") or ""))
            continue
        out.append(s)
    return "\n".join(out)


def detect_limit(lane: str, lane_log: Path) -> str | None:
    """The matching line if the tail of the lane log shows a usage/rate limit, else None."""
    pats = LIMIT_PATTERNS.get(lane) or [p for ps in LIMIT_PATTERNS.values() for p in ps]
    for line in reversed(_limit_text(lane_log).splitlines()):
        if any(re.search(p, line, re.I) for p in pats):
            return line.strip()
    return None


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                       "sep", "oct", "nov", "dec"), 1)}
_WHEN = r"(?:try again|resets?|available again)\s*(?:at|after|on)?\s*"
_DATE = r"(?:([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(?:(\d{4}),?)?\s*(?:at\s+)?)?"


def parse_limit_until(text: str, at: dt.datetime | None = None) -> dt.datetime | None:
    """Local time a limit message says to come back: 'try again at 10:55 PM', 'resets at 3am',
    'resets 3am (Europe/London)', 'try again in 20 minutes', 'Nov 3rd, 2026 10:55 PM', claude's
    old 'usage limit reached|<epoch>'. None if it names no usable time. A time zone in the
    message is ignored: the time is read as local."""
    at = at or now()
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return dt.datetime.fromtimestamp(int(m.group(1))).replace(microsecond=0)
    m = re.search(r"(?:try again|resets?|available again)\s+in\s+(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
                  text, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return at + dt.timedelta(seconds=n * (3600 if unit[0] == "h" else 1 if unit[0] == "s" else 60))
    m = (re.search(_WHEN + _DATE + r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\b", text, re.I)
         or re.search(_WHEN + _DATE + r"(\d{1,2}):(\d{2})()", text, re.I))
    if not m:
        return None
    mon, day, year, hh, mm, ampm = m.groups()
    h, mi = int(hh), int(mm or 0)
    if ampm:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ampm.lower() == "p" else 0)
    if h > 23 or mi > 59:
        return None
    if mon and mon.lower() in _MONTHS:
        try:
            when = dt.datetime(int(year) if year else at.year, _MONTHS[mon.lower()], int(day), h, mi)
        except ValueError:
            return None
        return when if when > at else None
    when = at.replace(hour=h, minute=mi, second=0, microsecond=0)
    return when if when > at else when + dt.timedelta(days=1)


def lane_limited(state: dict, lane: str) -> dt.datetime | None:
    """When a limited lane may run again, or None if it isn't limited (any more)."""
    until = (state.get("lanes", {}).get(lane) or {}).get("limited_until")
    if not until:
        return None
    when = dt.datetime.fromisoformat(until)
    return when if now() < when else None


def fallback_lane(task: Task, state: dict) -> str | None:
    """The lane to run on while the task's own lane is limited, if config and header allow."""
    if task.fields.get("lane-fixed", "").lower() in ("yes", "true", "1"):
        return None
    fb = (C.get("lane_fallback", {"codex": "claude"}) or {}).get(task.lane)
    if not fb or fb == task.lane or fb not in lanes() or lane_limited(state, fb):
        return None
    return fb


def route(tasks: list[Task], state: dict) -> list[Task]:
    """Move tasks off a limited lane onto its fallback; the header's lane is kept as lane-from."""
    for t in tasks:
        if "lane-from" not in t.fields and lane_limited(state, t.lane):
            fb = fallback_lane(t, state)
            if fb:
                t.fields["lane-from"], t.fields["lane"] = t.lane, fb
    return tasks


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
    until = lane_limited(state, task.lane)
    if until and not fallback_lane(task, state):
        return f"lane {task.lane} limited until {until:%H:%M}"
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

ALIVE_UNVERIFIED = 0  # proc_created(): the process exists but its creation time can't be read


def proc_created(pid: int) -> int | None:
    """The process's creation time (Windows FILETIME ticks; Linux start time in clock ticks),
    ALIVE_UNVERIFIED if it exists but we can't read that, None if no such process is running.
    Never signals it (os.kill would terminate on Windows)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)  # own instance: argtypes stay local
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ALIVE_UNVERIFIED if ctypes.get_last_error() == 5 else None  # 5: exists, not ours
        try:
            code = wintypes.DWORD()
            if k.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:  # STILL_ACTIVE
                return None
            t = [wintypes.FILETIME() for _ in range(4)]
            if not k.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
                return ALIVE_UNVERIFIED
            return (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
        finally:
            k.CloseHandle(h)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[19])  # field 22: starttime
    except (OSError, IndexError, ValueError):
        pass
    try:
        os.kill(pid, 0)
        return ALIVE_UNVERIFIED
    except ProcessLookupError:
        return None
    except PermissionError:
        return ALIVE_UNVERIFIED


def _alive(pid, created) -> bool:
    """The liveness check: a process with this pid exists AND it is the one the lock names.
    A reused pid (same number, different creation time) is not the old worker. A lock written
    before creation times were recorded (created missing) is judged by the pid alone."""
    got = proc_created(pid)
    if got is None:
        return False
    if not created or got == ALIVE_UNVERIFIED:
        return True
    return got == created


def read_lock(path: Path) -> dict | None:
    """The lock's JSON, or None if it can't be read or parsed (then only the age rule applies)."""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def lock_status(info: dict | None) -> str:
    """'unknown' (unparseable / no pid / ours: age rule only), 'live' (owner running),
    'orphan' (owner gone, its lane child still running) or 'dead' (both gone)."""
    if not info or not info.get("pid") or info.get("pid") == os.getpid():
        return "unknown"
    if _alive(info["pid"], info.get("created")):
        return "live"
    if info.get("lane_pid") and _alive(info["lane_pid"], info.get("lane_created")):
        return "orphan"
    return "dead"


def _my_lock(**extra) -> dict:
    return {"pid": os.getpid(), "created": proc_created(os.getpid()), "since": now().isoformat(), **extra}


def take_lock(state: dict) -> bool:
    """Atomic create. A lock whose owner (pid + creation time) is gone is stale at once, unless
    its lane child is still running; an unreadable lock is stale only once it is older than
    the longest possible run."""
    max_age = max(timeout_for(w, state) for w in ("light", "heavy")) + C["tick_watchdog_extra_s"]
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(_my_lock(), f)
            return True
        except FileExistsError:
            info = read_lock(LOCK_FILE)
            status = lock_status(info)
            if status in ("live", "orphan"):
                return False
            if status == "dead":
                # e.g. Task Scheduler killed the tick when the laptop went onto battery (2 Nov).
                # reconcile() has already collected a task the lock named.
                log(f"RECONCILE dead owner {info['pid']} for {info.get('task') or '-'} ({LOCK_FILE.name})")
                LOCK_FILE.unlink(missing_ok=True)
                continue
            try:
                age = time.time() - LOCK_FILE.stat().st_mtime
            except FileNotFoundError:
                continue
            if age < max_age:
                return False
            log(f"stale lock ({int(age)}s old) removed")
            LOCK_FILE.unlink(missing_ok=True)
    return False


def set_lock_task(slug: str, lane: str = "") -> None:
    LOCK_FILE.write_text(json.dumps(_my_lock(task=slug, lane=lane)), encoding="utf-8")


def update_lock(**extra) -> None:
    """Add fields (the lane child's pid and creation time) to this tick's lock."""
    info = read_lock(LOCK_FILE)
    if info is not None and info.get("pid") == os.getpid():
        info.update(extra)
        LOCK_FILE.write_text(json.dumps(info), encoding="utf-8")


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


# ---------------------------------------------------------------- combined-tree integration
# Two branches can each pass their own gate and still break master together (3 Claude slots
# + Codex at once, 2 Nov 2026). So the runner merges the branch onto *current* master in a
# separate integration worktree, re-runs the gate there, and only then fast-forwards master
# to that exact tested commit (master prompt section 14).

def dirty_clash(branch: str) -> list[str]:
    """Files with uncommitted changes in the main checkout that the branch also touches.
    Chuen's own unrelated edits (e.g. eval/dataset.jsonl) don't block a merge."""
    dirty = {l[3:].strip().strip('"') for l in
             git("status", "--porcelain", "--untracked-files=no").stdout.splitlines() if l.strip()}
    touched = set(git("diff", "--name-only", f"HEAD...{branch}", check=False).stdout.split())
    return sorted(dirty & touched)


def fast_checks(wt: Path) -> tuple[str, str]:
    """scripts/run_checks.py --fast in the integration tree, if the script supports it.
    Returns (verdict, detail) like run_gate; 'skipped' when there is nothing to run."""
    script = wt / "scripts" / "run_checks.py"
    if not script.exists():
        return "skipped", "no scripts/run_checks.py"
    if "--fast" not in script.read_text(encoding="utf-8", errors="replace"):
        return "skipped", "scripts/run_checks.py has no --fast option"
    venv_py = wt / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    py = str(venv_py) if venv_py.exists() else sys.executable
    try:
        r = subprocess.run([py, str(script), "--fast"], cwd=wt, capture_output=True, text=True,
                           timeout=C["gate_timeout_s"], encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return "broken", f"run_checks.py --fast timed out after {C['gate_timeout_s']}s"
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
        return "fail", f"run_checks.py --fast exited {r.returncode}: {' | '.join(tail)}"
    return "pass", "run_checks.py --fast exited 0"


def check_combined(task: Task, wt: Path) -> tuple[bool, list[str]]:
    """The task's gate plus run_checks --fast, in the integration tree. (ok, checks run)."""
    verdict, detail = run_gate(task, wt)
    checks = [f"gate: {verdict}: {detail}"]
    if verdict != "pass":
        return False, checks
    verdict, detail = fast_checks(wt)
    checks.append(f"run_checks --fast: {verdict}: {detail}")
    return verdict in ("pass", "skipped"), checks


def integration_tree(task: Task, base: str, branch: str) -> tuple[Path, str | None, str]:
    """A fresh detached worktree at `base` with `branch` merged in (--no-ff).
    Returns (path, merge commit or None, detail)."""
    wt = WORKTREES / f"{task.slug}--integration"
    if wt.exists():
        remove_worktree(wt, "", delete_branch=False)
    git("worktree", "prune", check=False)
    WORKTREES.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "--detach", str(wt), base)
    link_venv(wt)
    r = git("merge", "--no-ff", "-m", f"orchestra: merge {branch}", branch, cwd=wt, check=False)
    if r.returncode != 0:
        git("merge", "--abort", cwd=wt, check=False)
        return wt, None, "merge conflict; merge by hand"
    return wt, git("rev-parse", "HEAD", cwd=wt).stdout.strip(), "merged in the integration tree"


def integrate(task: Task, branch: str, rec: dict, on_merged=None) -> str:
    """Test branch + current master together, then fast-forward master to that tested commit.
    Fills `rec` (stored in state) with base, commit, checks and tries. Returns the merge
    outcome: 'merged', 'no commits', a merge-by-hand note, or one starting with 'parked:'.
    `on_merged` runs inside the merge lock right after a fast-forward (the aftercare push)."""
    ahead = git("rev-list", "--count", f"HEAD..{branch}", check=False).stdout.strip()
    if ahead in ("", "0"):
        return "no commits"
    clash = dirty_clash(branch)
    if clash:
        return f"main checkout has uncommitted changes to {', '.join(clash)}; merge by hand"
    rec["tries"] = 0
    for _ in range(2):  # master moved while testing -> redo once, then park
        rec["tries"] += 1
        base = git("rev-parse", "HEAD").stdout.strip()
        rec.update(base=base, commit=None, checks=[])
        try:
            wt, commit, detail = integration_tree(task, base, branch)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            return f"parked: could not build the integration tree: {(getattr(e, 'stderr', '') or str(e)).strip()[:200]}"
        rec["commit"] = commit
        try:
            if commit is None:
                return detail
            ok, rec["checks"] = check_combined(task, wt)
            log(f"integration {task.slug} on {base[:8]}: {'pass' if ok else 'FAIL'}: {'; '.join(rec['checks'])}")
            if not ok:
                return ("parked: the combined tree (this branch merged onto current master "
                        f"{base[:8]}) fails: {rec['checks'][-1]}")  # the failing check is the last
        finally:
            remove_worktree(wt, "", delete_branch=False)
        with _FileLock(REVIEW / "merge.lock", wait=600, stale=900):  # two lanes may finish together
            if git("rev-parse", "HEAD").stdout.strip() != base:
                log(f"integration {task.slug}: master moved while testing (was {base[:8]})")
                continue
            clash = dirty_clash(branch)
            if clash:
                return f"main checkout has uncommitted changes to {', '.join(clash)}; merge by hand"
            r = git("merge", "--ff-only", commit, check=False)
            if r.returncode != 0:
                return f"fast-forward to the tested merge failed: {(r.stderr or r.stdout).strip()[:200]}; merge by hand"
            if on_merged:
                on_merged()
            return "merged"
    return "parked: master moved twice, retry"


# ---------------------------------------------------------------- reviewer lane
# Nothing reviewed a diff except the session that wrote it. Risky diffs get a fresh, read-only
# claude -p before integration; its `VERDICT: block` parks the task.

REVIEW_DIFF_PROMPT = HERE / "review_diff_prompt.md"
DEFAULT_REVIEWER = ["claude", "-p", "--permission-mode", "default", "--permission-prompts", "none",
                    "--output-format", "json",
                    "--allowedTools", "Read,Glob,Grep,Bash(git diff:*),Bash(git log:*)",
                    "--disallowedTools", "Edit,Write,MultiEdit,NotebookEdit,Bash(git push:*),Bash(gh:*)"]
RISKY_LINES = 400
REVIEW_DIFF_MAX_CHARS = 150_000


def risk(branch: str) -> str | None:
    """Why this branch's diff (vs master) needs a reviewer, or None if it doesn't."""
    files = git("diff", "--name-only", f"HEAD...{branch}", check=False).stdout.split()
    hits = [f for f in files if f.startswith("orchestra/") or f == "atrium/hook.py"
            or ".git/hooks" in f or re.fullmatch(r"install_[^/]*\.ps1", f.rsplit("/", 1)[-1])]
    lines = 0
    for row in git("diff", "--numstat", f"HEAD...{branch}", check=False).stdout.splitlines():
        a, d, *_ = row.split("\t") + ["", ""]
        lines += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    why = []
    if hits:
        why.append("touches " + ", ".join(hits[:8]) + (" ..." if len(hits) > 8 else ""))
    if lines > int(C.get("review_max_lines", RISKY_LINES)):
        why.append(f"{lines} changed lines (over {int(C.get('review_max_lines', RISKY_LINES))})")
    return "; ".join(why) or None


def parse_verdict(text: str) -> tuple[str | None, str]:
    """('ok'|'block'|None, reasons) from the reviewer's output (claude json or plain text)."""
    s = text.strip()
    try:
        data = json.loads(s if s.startswith("{") else s[s.rfind("\n{") + 1:])
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            s = data["result"]
    except ValueError:
        pass
    m = re.search(r"^\W*VERDICT:\W*(ok|block)\b(.*)", s, re.I | re.M | re.S)
    if not m:
        return None, s[-500:]
    return m.group(1).lower(), m.group(2).strip()[:2000]


def run_reviewer(task: Task, wt: Path, branch: str, report: Path, why: str) -> dict:
    """A fresh read-only reviewer lane on the actual diff. Returns {verdict, reasons, seconds,
    cost_usd, log}. Fails closed: no verdict, a timeout or a missing lane is a block."""
    argv = list(lanes().get("reviewer") or DEFAULT_REVIEWER)
    exe = shutil.which(argv[0]) or argv[0]
    if not Path(exe).exists() and not shutil.which(exe):
        return {"verdict": "block", "reasons": f"reviewer lane missing: {argv[0]} not on PATH",
                "seconds": 0, "cost_usd": None}
    base = git("merge-base", "HEAD", branch, check=False).stdout.strip() or "HEAD"
    diff = git("diff", f"{base}...{branch}", check=False).stdout
    if len(diff) > REVIEW_DIFF_MAX_CHARS:
        diff = diff[:REVIEW_DIFF_MAX_CHARS] + f"\n[... diff cut at {REVIEW_DIFF_MAX_CHARS} chars; run `git diff {base[:12]}...HEAD` for the rest]\n"
    read = lambda p: p.read_text(encoding="utf-8", errors="replace") if p.exists() else "(none)"
    prompt = REVIEW_DIFF_PROMPT.read_text(encoding="utf-8")
    for k, v in (("TASK", read(task.path)), ("REPORT", read(report)), ("BRANCH", branch),
                 ("BASE", base), ("WHY", why), ("DIFF", diff)):  # DIFF last: it may contain {{...}}
        prompt = prompt.replace("{{" + k + "}}", v)
    if Path(argv[0]).stem.lower() == "claude":
        (wt / ".review").mkdir(exist_ok=True)
        argv += ["--settings", str(hook_settings(wt))]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")
           and k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}
    env.update(ORCHESTRA_WORKTREE=str(wt), ORCHESTRA_MAIN=str(REPO), ORCHESTRA_REVIEW="1")
    log_path = REPORTS / f"{task.slug}.review.log"
    timeout = int(C.get("review_timeout_s", 900))
    start = time.time()
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with log_path.open("w", encoding="utf-8") as out:
        proc = subprocess.Popen([exe, *argv[1:]], cwd=wt, env=env, stdin=subprocess.PIPE, stdout=out,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", **kw)
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            timed_out = True
    seconds = int(time.time() - start)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    cost = lane_result(log_path).get("cost_usd")
    if timed_out:
        return {"verdict": "block", "reasons": f"reviewer timed out after {timeout}s", "seconds": seconds,
                "cost_usd": cost}
    verdict, reasons = parse_verdict(text)
    if verdict is None:
        return {"verdict": "block", "reasons": "reviewer gave no VERDICT line: " + reasons[-300:],
                "seconds": seconds, "cost_usd": cost}
    return {"verdict": verdict, "reasons": reasons, "seconds": seconds, "cost_usd": cost}


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
        shim = Path(os.environ.get("APPDATA", "")) / "npm" / f"{argv[0]}.cmd"
        return (f"LANE_MISSING: {argv[0]} not on PATH (which={shutil.which(argv[0])!r}, "
                f"{shim} exists={shim.exists()}, file={__file__})"), 0
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
    # ANTHROPIC_API_KEY / AUTH_TOKEN would make claude bill the API instead of Chuen's Max plan
    # (his .env holds a key for Denisse's own code); never pass them to a lane (2 Nov 2026).
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")
           and k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}
    env.update(ORCHESTRA_TASK=str(local / "task.md"), ORCHESTRA_REPORT=str(local_report),
               ORCHESTRA_WORKTREE=str(wt), ORCHESTRA_MAIN=str(REPO))
    out = (REPORTS / f"{task.slug}.lane.log").open("w", encoding="utf-8")
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    start = time.time()
    proc = subprocess.Popen([exe, *argv[1:]], cwd=wt, env=env, stdin=subprocess.PIPE,
                            stdout=out, stderr=subprocess.STDOUT, text=True, encoding="utf-8", **kw)
    # So a later tick can tell an orphaned lane (this runner killed, the child not) from a dead one.
    update_lock(lane_pid=proc.pid, lane_created=proc_created(proc.pid), lane_since=now().isoformat())
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


def charge(state: dict, task: Task, seconds: int, cost: float | None, lane: str | None = None) -> str:
    """Add a lane run's time and cost to today's usage. `lane` overrides task.lane (the reviewer)."""
    u = today_usage(state)
    u["maint_s" if task.kind == "maintenance" else "work_s"] += seconds
    if cost is not None:
        u["cost_usd"] += cost
        u["known_cost_s"] += seconds
        return f"${cost:.2f}"
    if (lane or task.lane) != "codex" and seconds:  # codex reports no cost; it runs on its own plan
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
    if info["result"] in ("NO_REPORT", "TIMEOUT"):
        hit = detect_limit(task.lane, REPORTS / f"{task.slug}.lane.log")
        if hit:
            return _limited(task, state, ts, hit, seconds, spent)
    stray = sorted(main_checkout_status() - before_status)
    hooks_loaded = task.lane != "claude" or (wt / ".review" / "hook.log").exists()

    entry = {"end": now().isoformat(), "result": info["result"], "seconds": seconds, "lane": task.lane,
             "weight": task.weight, "note": info["note"], "premise": info["premise"], "spent": spent,
             "denied": lane_info.get("denied", []), "main_checkout_changed": stray,
             "hooks_loaded": hooks_loaded}
    if task.fields.get("lane-from"):
        entry["fallback_from"] = task.fields["lane-from"]
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

        # Reviewer lane: risky diffs get a fresh read-only reviewer before integration.
        why_risky = risk(branch) if C.get("review_risky", True) else None
        if why_risky:
            log(f"review {task.slug}: {why_risky}")
            rv = run_reviewer(task, wt, branch, report, why_risky)
            rv["spent"] = charge(state, task, rv["seconds"], rv.pop("cost_usd", None), lane="claude")
            rv["why"] = why_risky
            log(f"review {task.slug}: {rv['verdict']} after {rv['seconds']}s ({rv['spent']})")
        else:
            rv = {"verdict": "skipped", "why": "review_risky is off" if not C.get("review_risky", True)
                  else "not risky"}
        ts["review"] = entry["review"] = rv
        if rv["verdict"] == "block":
            PARKED.mkdir(parents=True, exist_ok=True)
            (PARKED / f"{task.slug}.review.md").write_text(
                f"# Reviewer: VERDICT: block\nWhy reviewed: {why_risky}\n\n{rv['reasons']}\n", encoding="utf-8")
            park(task, f"reviewer blocked it ({rv['reasons'].splitlines()[0][:200] if rv['reasons'] else 'no reason'});"
                       f" review in {task.slug}.review.md. Branch {branch} kept.", report)
            return "REVIEW-BLOCKED"

        ts.pop("push", None)

        def push():
            if C.get("auto_push") and aftercare_on():
                try:
                    import aftercare
                    ts["push"] = aftercare.push(REPO)
                except Exception as e:  # a push problem must never lose the merge
                    ts["push"] = f"push step crashed: {e}"
                log(f"push {task.slug}: {ts['push']}")

        integ: dict = {}
        merge = integrate(task, branch, integ, on_merged=push)
        ts["integration"] = entry["integration"] = integ
        log(f"merge {branch}: {merge}")
        if merge.startswith("parked:"):
            ts.pop("merge", None)  # the park reason already says it; STATE NOW would list it twice
            park(task, f"integration: {merge[len('parked:'):].strip()}. Master untouched; branch {branch} kept.", report)
            return "INTEGRATION-REJECTED"
        ts["merge"] = merge
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


def _limited(task: Task, state: dict, ts: dict, hit: str, seconds: int, spent: str) -> str:
    """The lane hit its usage limit: not an attempt. Record when the lane may run again."""
    until = parse_limit_until(hit) or now() + dt.timedelta(minutes=LIMIT_DEFAULT_MIN)
    state.setdefault("lanes", {})[task.lane] = {"limited_until": until.isoformat(),
                                                "since": now().isoformat(), "message": hit[:300]}
    ts["attempts"] = max(0, ts["attempts"] - 1)
    note = f"lane {task.lane} limited until {until:%Y-%m-%d %H:%M}: {hit[:200]}"
    entry = {"end": now().isoformat(), "result": "LIMITED", "seconds": seconds, "lane": task.lane,
             "weight": task.weight, "note": note, "premise": "not stated", "spent": spent}
    ts["history"].append(entry)
    ts.update(last_end=entry["end"], last_result="LIMITED")
    log(f"LIMITED {task.slug} lane={task.lane} until {until.isoformat(sep=' ', timespec='minutes')}"
        f" (attempt not counted): {hit[:200]}")
    return "LIMITED"


def aftercare_on() -> bool:
    """Vault notes and pushes are for the real repo only, never a test's temp repo."""
    return os.environ.get("ORCHESTRA_AFTERCARE") == "1" or "ORCHESTRA_REPO" not in os.environ


def write_vault_note(task: Task, ts: dict, result: str) -> None:
    """One section per finished task in <vault>/Calendar/Orchestra/<date>.md (Chuen, 2 Nov 2026)."""
    vault = Path(os.path.expandvars(os.path.expanduser(C.get("vault_dir", ""))))
    if not C.get("vault_dir") or not aftercare_on() or not vault.is_dir():
        return
    try:
        import aftercare
        h = ts["history"][-1] if ts.get("history") else {}
        day = ARCHIVE / now().date().isoformat()
        where = day if result == "PASS" else PARKED
        task_file = where / f"{task.slug}.md"
        report_file = where / f"{task.slug}.report.md"
        why_file = PARKED / f"{task.slug}.why.txt"
        why = ""
        if result == "LIMITED":
            why = h.get("note", "")
        elif result != "PASS" and why_file.exists():
            why = why_file.read_text(encoding="utf-8").splitlines()[0].split(" ", 1)[-1]
        lane = task.lane
        if task.fields.get("lane-from"):
            lane += f" (fallback: lane {task.fields['lane-from']} was limited)"
        read = lambda p: p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        task_text = read(task_file) or read(task.path)
        title = (task_text.splitlines() or [task.slug])[0].lstrip("# ").strip() or task.slug
        aftercare.vault_note(
            vault, slug=task.slug, title=title, result=result, lane=lane,
            seconds=h.get("seconds", 0), spent=h.get("spent", "?"), merge=ts.get("merge", "") if result == "PASS" else "",
            pushed=ts.get("push", "") if result == "PASS" else "", why=why, task_text=task_text,
            report_text=read(report_file),
            report_path=str(report_file.relative_to(REPO)) if report_file.exists() else "")
    except Exception as e:  # the vault note must never break the runner
        log(f"vault note failed for {task.slug}: {e}")


def reconcile(state: dict) -> dict:
    """Before dispatching: collect what a killed tick left behind (2 Nov 2026: on battery, Task
    Scheduler ended both runner ticks but not their claude -p children). For each slot lock whose
    owner is gone and which names a task:
      * its lane child still runs -> leave the slot occupied; the task isn't dispatched again
      * child gone, worktree has .review/report.md -> finish() it as if the lane had exited
      * child gone, no report -> finish() records NO_REPORT under the INFRA retry rule
    Worktrees are kept; unreadable locks are left to take_lock's age rule.
    Returns {slug: outcome}."""
    out: dict = {}
    for lf in sorted(REVIEW.glob("runner*.lock")):
        info = read_lock(lf)
        status = lock_status(info)
        slug = (info or {}).get("task")
        if status == "orphan":
            log(f"RECONCILE orphan lane {info['lane_pid']} still running {slug or '-'}")
            out[slug or lf.name] = "orphan running"
            continue
        if status != "dead" or not slug:
            continue  # live, unreadable, or no task named (take_lock just removes those)
        try:  # claim it: one reconciler per lock; our live pid then keeps other ticks off the slot
            with _FileLock(REVIEW / "reconcile.lock", wait=30, stale=120):
                if read_lock(lf) != info:
                    continue
                log(f"RECONCILE dead owner {info['pid']} for {slug}")
                lf.write_text(json.dumps(_my_lock(task=slug, lane=info.get("lane", ""), reconciling=True)),
                              encoding="utf-8")
        except TimeoutError:
            continue
        try:
            out[slug] = _collect(state, info, slug)
        except Exception as e:  # never let one bad leftover stop the tick
            log(f"RECONCILE {slug} failed: {e!r}; worktree kept")
            out[slug] = f"error: {e}"
        finally:
            lf.unlink(missing_ok=True)
    return out


def _collect(state: dict, info: dict, slug: str) -> str:
    path = QUEUE / f"{slug}.md"
    if not path.exists():
        log(f"RECONCILE {slug}: task no longer in the queue; nothing to collect, worktree kept")
        return "not queued"
    task = parse_task(path)
    if info.get("lane") and info["lane"] != task.lane:  # it ran on a fallback lane
        task.fields["lane-from"], task.fields["lane"] = task.lane, info["lane"]
    wt, branch = WORKTREES / slug, f"review/{slug}"
    local_report, report = wt / ".review" / "report.md", REPORTS / f"{slug}.md"
    report.unlink(missing_ok=True)
    if local_report.exists():
        shutil.copyfile(local_report, report)
    try:
        started = dt.datetime.fromisoformat(info.get("lane_since") or info.get("since"))
        seconds = max(0, int((now() - started).total_seconds()))
    except (TypeError, ValueError):
        seconds = 0
    before = set(info["before"]) if isinstance(info.get("before"), list) else main_checkout_status()
    log(f"RECONCILE collecting {slug}: lane gone, report {'found' if local_report.exists() else 'missing'}")
    ts = task_state(state, task)
    n = len(ts["history"])
    result = finish(task, state, wt, branch, "ran", seconds, before)
    for entry in ts["history"][n:]:
        entry["recovered"] = True
    save_state(state)
    write_vault_note(task, ts, result)
    return result


def tick(dry_run: bool = False) -> str:
    for d in (QUEUE, REPORTS, ARCHIVE, PARKED):
        d.mkdir(parents=True, exist_ok=True)
    state = load_state()
    tasks = route(load_queue(), state)  # off a limited lane onto its fallback, if allowed
    if dry_run:
        for t in tasks:
            print(f"{t.slug}: {why_not_runnable(t, state) or 'runnable'}"
                  f" [{t.priority}/{t.weight}/{t.kind}/{t.lane}, gate commands: {len(t.gate_commands)}]")
        t = pick(tasks, state)
        print(f"would run: {t.slug if t else 'nothing'}; spend left today ${spend_left(state):.2f}")
        return "dry-run"
    global LOCK_FILE, _BASE
    parallel = bool(C.get("parallel_lanes") and aftercare_on())
    if parallel:
        _BASE = json.loads(json.dumps(state))
    # Reconcile before dispatching: a killed tick's orphaned lane or leftover report.
    orphans = {s for s, r in reconcile(state).items() if r == "orphan running"}
    tasks = [t for t in route(load_queue(), state) if t.slug not in orphans]
    if parallel:
        # One runner per lane at a time (Chuen, 2 Nov 2026: use Claude and Codex together).
        # Each lane has its own lock; state saves merge, and merges are serialised.
        order = []
        for t in sorted((t for t in tasks if why_not_runnable(t, state) is None),
                        key=lambda t: (t.priority != "urgent", t.kind == "maintenance", t.slug)):
            if t.lane not in order:
                order.append(t.lane)
        slots = C.get("lane_slots", {})
        # Tasks another slot of the same lane is already running (named in its lock file).
        running = set()
        for lf in REVIEW.glob("runner.*.lock"):
            try:
                running.add(json.loads(lf.read_text(encoding="utf-8")).get("task"))
            except (OSError, ValueError):
                pass
        chosen = None
        for lane in order:
            for slot in range(1, int(slots.get(lane, 1)) + 1):
                LOCK_FILE = REVIEW / (f"runner.{lane}.lock" if slot == 1 else f"runner.{lane}.{slot}.lock")
                if take_lock(state):
                    chosen = lane
                    break
            if chosen:
                tasks = [t for t in tasks if t.lane == lane and t.slug not in running]
                break
        else:
            return "busy" if order else "idle"
    elif not take_lock(state):
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
        set_lock_task(task.slug, task.lane)
        fb = ""
        if task.fields.get("lane-from"):
            fb = (f" (fallback: lane {task.fields['lane-from']} limited until "
                  f"{lane_limited(state, task.fields['lane-from']):%H:%M})")
        log(f"START {task.slug} lane={task.lane}{fb} weight={task.weight} timeout={timeout}s attempt={ts['attempts']}")
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
        update_lock(before=sorted(before))  # reconcile() needs it to judge stray writes
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
        write_vault_note(task, ts, result)
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
