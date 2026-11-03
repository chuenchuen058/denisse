"""Soak harness: a throwaway repo, ~40 planned tasks on fake lanes, concurrent ticks with
crashes and power flips, and invariants checked at the end. Stdlib only.

    .venv\\Scripts\\python.exe -m orchestra.soak --minutes 5 --seed 1
    .venv\\Scripts\\python.exe -m orchestra.soak --minutes 2 --seed 1 --keep   # keep the temp dir

The runner under test is the real orchestra/runner.py of this checkout, started as a separate
process per tick (like Task Scheduler's Parallel instances), with ORCHESTRA_REPO pointing at
the temp repo and ORCHESTRA_AFTERCARE=1 so parallel lanes and the guarded push are exercised
against a bare "origin" in the same temp dir. vault_dir is "" so no vault note is written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent  # orchestra/soak
ORCH = HERE.parent  # orchestra
sys.path.insert(0, str(ORCH))
import aftercare  # noqa: E402  PRIVATE_PATHS: what the push must never have let through
import campaign  # noqa: E402  pause() before the final reconcile tick
import runner as R  # noqa: E402  liveness helpers only; ticks run runner.py as subprocesses

LANES = ("alpha", "beta", "gamma")
SLOTS = {"alpha": 2, "beta": 1, "gamma": 2}
FALLBACK = {"beta": "alpha"}
TIMEOUTS = {"light": 20, "heavy": 60}
INVARIANTS = ("no_concurrent_runs", "none_lost", "state_sums", "no_dead_locks", "master_merges_gated",
              "origin_gated", "no_orphans", "worktrees", "ticks_clean")
GIT_ID = {"GIT_AUTHOR_NAME": "soak", "GIT_AUTHOR_EMAIL": "soak@example.invalid",
          "GIT_COMMITTER_NAME": "soak", "GIT_COMMITTER_EMAIL": "soak@example.invalid"}

GATE_SCRIPT = textwrap.dedent('''\
    """Soak gate: checks on the tree that two individually passing branches can break together."""
    import pathlib, re, sys
    root = pathlib.Path.cwd()
    bad = []
    hot = (root / "hot.md").read_text(encoding="utf-8") if (root / "hot.md").exists() else ""
    if re.search(r"^(<{7}|={7}|>{7})( |$)", hot, re.M):
        bad.append("conflict markers in hot.md")
    if (root / "broken.flag").exists():
        bad.append("broken.flag present")
    n = len(list((root / "budget").glob("*.txt"))) if (root / "budget").is_dir() else 0
    if n > 2:
        bad.append(f"budget/ has {n} entries, max 2")
    for p in (root / "work").glob("*.txt") if (root / "work").is_dir() else []:
        if not p.read_text(encoding="utf-8").strip():
            bad.append(f"{p.name} is empty")
    print("soak gate:", "ok" if not bad else "; ".join(bad))
    sys.exit(1 if bad else 0)
''')

TASK = """# soak {n:02d}: {title}
Queue: auto
Priority: {priority}
Weight: {weight}
Kind: {kind}
Lane: {lane}
Written: {written}
Soak: {plan}
{fixed}
Fake task {n} of a soak run (seed {seed}). The `Soak:` line above tells the fake lane what to do.

## Gate
```
"{python}" tools/soak_gate.py
```
"""


# ---------------------------------------------------------------- plans

def make_plans(seed: int, n: int) -> list[dict]:
    """Deterministic per seed: what each of the n fake tasks does and how it ends."""
    rng = random.Random(seed)
    plans = []
    for i in range(n):
        p = {"slug": f"soak-{seed}-{i:02d}", "sleep": round(rng.uniform(0.5, 4.0), 1),
             "cost": round(rng.uniform(0.01, 0.5), 2), "result": "PASS", "title": "plain pass"}
        roll = rng.random()
        if roll < 0.50:
            pass
        elif roll < 0.57:
            p.update(result="PARTIAL", title="partial")
        elif roll < 0.63:
            p.update(result="FAIL", title="fail")
        elif roll < 0.69:
            p.update(result="NONE", title="no report")
        elif roll < 0.73:
            p.update(human="needs Chuen's API key", title="human question")
        elif roll < 0.80:
            p.update(limit=True, limit_s=20, title="rate limit once")
        elif roll < 0.86:
            p.update(hot=True, title="hot file")
        elif roll < 0.92:
            p.update(risky=True, review=rng.choice(("ok", "ok", "block")), title="risky diff")
        elif roll < 0.96:
            p.update(budget=True, title="budget entry")
        else:
            p.update(bad_gate=True, title="breaks the gate")
        if rng.random() < 0.12:
            p["hot"] = True
        if rng.random() < 0.15:
            p["grandchild"] = True
        if n >= 10 and i == n // 3:
            p.update(stray=True, title=p["title"] + ", writes to main checkout")
        if n >= 10 and i == n - 3:  # late, so earlier merges still get pushed; afterwards nothing may be
            p.update(private=True, title=p["title"] + ", private path", priority="normal")
        if n >= 10 and i == n - 1:
            p.update(sleep=300, title="hangs past its timeout", weight="light")
        p.setdefault("lane", rng.choice(LANES))
        p.setdefault("weight", "heavy" if rng.random() < 0.25 else "light")
        p.setdefault("priority", "urgent" if rng.random() < 0.10 else "normal")
        p["kind"] = "maintenance" if rng.random() < 0.15 else "work"
        p["fixed"] = rng.random() < 0.10
        plans.append(p)
    if n >= 20:  # every path at least once, whatever the dice did (still deterministic per seed)
        plain = [p for p in plans if p["title"] == "plain pass" and p["sleep"] < 100]
        for key, extra in (("limit", {"limit_s": 20}), ("hot", {}), ("risky", {"review": "ok"}),
                           ("risky", {"review": "block"}), ("budget", {}), ("budget", {}), ("bad_gate", {}),
                           ("human", {"human": "needs Chuen's API key"}), ("grandchild", {})):
            if key == "risky" and sum(p.get("review") == extra["review"] for p in plans) or \
                    key != "risky" and sum(bool(p.get(key)) for p in plans) >= (2 if key == "budget" else 1):
                continue
            if not plain:
                break
            p = plain.pop(rng.randrange(len(plain)))
            p.update({key: True, "title": key.replace("_", " ")}, **extra)
    return plans


def task_text(plan: dict, n: int, seed: int, python: str) -> str:
    fields = {k: v for k, v in plan.items() if k not in ("lane", "weight", "priority", "kind", "fixed", "title")}
    return TASK.format(n=n, title=plan["title"], priority=plan["priority"], weight=plan["weight"],
                       kind=plan["kind"], lane=plan["lane"], written=dt.datetime.now().strftime("%Y-%m-%dT%H:%M"),
                       plan=json.dumps(fields, sort_keys=True), fixed="Lane-Fixed: yes" if plan["fixed"] else "",
                       seed=seed, python=python.replace("\\", "/"))


# ---------------------------------------------------------------- the run

class Soak:
    def __init__(self, minutes: float = 5, seed: int = 1, tasks: int = 40, procs: int = 6,
                 kill_every: float = 20.0, flip_every: float = 15.0, keep: bool = False, quiet: bool = False):
        self.minutes, self.seed, self.n, self.procs = minutes, seed, tasks, procs
        self.kill_every, self.flip_every, self.keep, self.quiet = kill_every, flip_every, keep, quiet
        self.rng = random.Random(seed * 7919 + 1)
        self.root: Path | None = None
        self.plans = make_plans(seed, tasks)
        self.ticks: list[dict] = []
        self.lock_dups: list[str] = []
        self.power = "ac"
        self.findings: list[dict] = []
        self.started = self.ended = 0.0

    # --- paths
    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def review(self) -> Path:
        return self.repo / ".review"

    @property
    def worktrees(self) -> Path:
        return self.root / "repo-wt"

    def say(self, msg: str) -> None:
        if not self.quiet:
            print(f"[soak {time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        r = subprocess.run(["git", *args], cwd=cwd or self.repo, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env={**os.environ, **GIT_ID}, timeout=120)
        if check and r.returncode:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
        return r.stdout.strip()

    # --- setup
    def setup(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix=f"orchestra-soak-{self.seed}-"))
        self.repo.mkdir()
        self.git("init", "-q", "-b", "master")
        (self.repo / ".gitignore").write_text(".review/*\n.venv\n", encoding="utf-8")
        (self.repo / "README.md").write_text("# soak repo\n", encoding="utf-8")
        (self.repo / "hot.md").write_text("# hot\n" + "".join(f"line {i}\n" for i in range(10)), encoding="utf-8")
        (self.repo / "tools").mkdir()
        (self.repo / "tools" / "soak_gate.py").write_text(GATE_SCRIPT, encoding="utf-8")
        (self.repo / "orchestra").mkdir()
        shutil.copy(ORCH / "review_prompt.md", self.repo / "orchestra" / "review_prompt.md")
        (self.repo / "orchestra" / "notes.txt").write_text("notes\n", encoding="utf-8")
        (self.repo / "work").mkdir()
        (self.repo / "work" / ".keep").write_text("", encoding="utf-8")
        self.git("add", "--", ".gitignore", "README.md", "hot.md", "tools/soak_gate.py",
                 "orchestra/review_prompt.md", "orchestra/notes.txt", "work/.keep")
        self.git("commit", "-q", "-m", "soak: initial tree")
        self.base = self.git("rev-parse", "HEAD")
        origin = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(origin))
        self.git("push", "-q", "-u", "origin", "master")
        self.git("remote", "set-head", "origin", "master")
        (self.repo / ".venv").mkdir()
        (self.repo / ".venv" / "keep.txt").write_text("fake venv, linked into every worktree", encoding="utf-8")
        self.review.mkdir()
        lane = str(HERE / "fake_lane.py")
        lanes = {name: [sys.executable, lane, "--lane", name] for name in LANES}
        lanes["reviewer"] = [sys.executable, lane, "--reviewer"]
        (self.review / "lanes.json").write_text(json.dumps(lanes, indent=1), encoding="utf-8")
        (self.review / "queue").mkdir()
        for i, plan in enumerate(self.plans):
            (self.review / "queue" / f"{plan['slug']}.md").write_text(
                task_text(plan, i, self.seed, sys.executable), encoding="utf-8")
        for d in ("events", "ticks"):
            (self.root / d).mkdir()
        self.say(f"repo {self.repo}: {self.n} tasks, lanes {SLOTS}, seed {self.seed}")

    def env(self, power: str) -> dict:
        e = {k: v for k, v in os.environ.items()
             if not k.startswith("ORCHESTRA_") and k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")}
        e.update(ORCHESTRA_REPO=str(self.repo), ORCHESTRA_AFTERCARE="1", ORCHESTRA_VAULT_DIR="",
                 ORCHESTRA_LANE_SLOTS=json.dumps(SLOTS), ORCHESTRA_LANE_FALLBACK=json.dumps(FALLBACK),
                 ORCHESTRA_TIMEOUT_S=json.dumps(TIMEOUTS), ORCHESTRA_GATE_TIMEOUT_S="60",
                 # after 20 measured runs the timeout becomes max(floor, 3 x p95); the default floor
                 # of 300 s let the "hang" task run five minutes past the end of a 2-minute soak
                 ORCHESTRA_TIMEOUT_FLOOR_S=str(TIMEOUTS["light"]),
                 ORCHESTRA_BACKOFF_MIN="0", ORCHESTRA_DAILY_SPEND_CAP_USD="1000", ORCHESTRA_POWER=power,
                 # AFTERCARE=1 also switches the daily exams on, and those reach the real agents
                 # (CI's network guard caught a connect to Ollama, 3 Nov 2026): never in a soak
                 ORCHESTRA_EXAMS_DAILY_AT="",
                 SOAK_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE="1", **GIT_ID)
        return e

    # --- driving ticks
    def spawn_tick(self) -> dict:
        n = len(self.ticks)
        out = (self.root / "ticks" / f"{n:04d}.out").open("w", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, str(ORCH / "runner.py")], cwd=self.repo, env=self.env(self.power),
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        t = {"n": n, "proc": proc, "out": out, "started": time.time(), "ended": None, "power": self.power,
             "killed": False, "code": None}
        self.ticks.append(t)
        return t

    def reap(self) -> list[dict]:
        live = []
        for t in self.ticks:
            if t["ended"] is None:
                code = t["proc"].poll()
                if code is None:
                    live.append(t)
                else:
                    t.update(ended=time.time(), code=code)
                    t["out"].close()
        return live

    def poll_locks(self) -> None:
        """Two slot locks naming the same task at the same instant = the task runs twice."""
        by_task: dict[str, list[str]] = {}
        for lf in self.review.glob("runner*.lock"):
            info = R.read_lock(lf)
            if info and info.get("task"):
                by_task.setdefault(info["task"], []).append(lf.name)
        for slug, names in by_task.items():
            if len(names) > 1:
                line = f"{slug} named by {sorted(names)}"
                if line not in self.lock_dups:
                    self.lock_dups.append(line)

    def queue_left(self) -> int:
        return len(list((self.review / "queue").glob("*.md")))

    def run(self) -> None:
        self.started = time.time()
        deadline = self.started + self.minutes * 60
        last_kill = last_flip = last_say = self.started
        battery_since = None
        spawning = True
        while True:
            now = time.time()
            live = self.reap()
            self.poll_locks()
            if spawning and (now >= deadline or self.queue_left() == 0):
                spawning = False
                self.say("time is up" if now >= deadline else "queue empty" + "; draining running ticks")
            if not spawning and not live:
                break
            if not spawning and now > deadline + TIMEOUTS["heavy"] + 90:
                for t in live:  # a tick still running this long after the end is itself a finding
                    t["drain_task"] = [R.read_lock(lf).get("task") for lf in self.review.glob("runner*.lock")
                                       if R.read_lock(lf) and R.read_lock(lf).get("pid") == t["proc"].pid]
                    t["proc"].kill()
                    t["drain_killed"] = True
                self.reap()
                break
            if spawning:
                if len(live) < self.procs:
                    self.spawn_tick()
                if now - last_kill >= self.kill_every:
                    victims = [t for t in live if now - t["started"] > 2.0]
                    if victims:
                        v = self.rng.choice(victims)
                        v["proc"].kill()
                        v["killed"] = True
                        last_kill = now
                        self.say(f"killed tick {v['n']} after {now - v['started']:.0f}s")
                if self.power == "ac" and now - last_flip >= self.flip_every:
                    self.power, battery_since, last_flip = "battery:50", now, now
                elif self.power != "ac" and now - battery_since >= min(5.0, self.flip_every / 3):
                    self.power, last_flip = "ac", now
            if now - last_say >= 15:
                last_say = now
                self.say(f"t={now - self.started:.0f}s ticks={len(self.ticks)} live={len(live)} power={self.power} "
                         f"queue={self.queue_left()} parked={len(list((self.review / 'parked').glob('*.why.txt')))} "
                         f"archived={len(list((self.review / 'archive').glob('*/*.report.md'))) if (self.review / 'archive').exists() else 0}")
            time.sleep(0.25 if spawning else 1.0)
        # One more tick, on AC, with dispatch paused: it must reconcile what the kills left and
        # start nothing new. Then a second one, which must find nothing to do.
        self.power = "ac"
        campaign.pause(self.review)
        for _ in range(2):
            t = self.spawn_tick()
            t["final"] = True
            t["proc"].wait(timeout=TIMEOUTS["heavy"] + 120)
        self.reap()
        self.ended = time.time()

    # --- invariants
    def check(self) -> list[dict]:
        self.findings = []
        for name in INVARIANTS:
            try:
                details = list(getattr(self, "inv_" + name)())
            except Exception as e:  # a broken check is a failed check, never a pass
                details = [f"check crashed: {e!r}"]
            self.findings.append({"name": name, "ok": not details, "details": details})
        return self.findings

    def events(self, kind: str | None = None) -> list[dict]:
        out = []
        for p in sorted((self.root / "events").glob("*.json")):
            try:
                ev = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if kind is None or ev.get("kind") == kind:
                out.append(ev)
        return out

    def state(self) -> dict:
        return json.loads((self.review / "state.json").read_text(encoding="utf-8"))

    def inv_no_concurrent_runs(self):
        for ev in self.events("DUPLICATE"):
            yield f"lane for {ev['slug']} found another live lane (pid {ev['other_pid']}) on the same task"
        for line in self.lock_dups:
            yield f"two slot locks named the same task: {line}"

    def inv_none_lost(self):
        archive = self.review / "archive"
        for plan in self.plans:
            slug = plan["slug"]
            places = [p for p in (self.review / "queue" / f"{slug}.md", self.review / "parked" / f"{slug}.md",
                                  *(archive.glob(f"*/{slug}.md") if archive.exists() else [])) if p.exists()]
            if len(places) != 1:
                yield f"{slug}: found in {[str(p.relative_to(self.review)) for p in places] or 'nowhere'}"

    @staticmethod
    def _money(spent: str) -> tuple[float | None, bool]:
        m = re.search(r"\$([0-9.]+)", spent or "")
        return (float(m.group(1)) if m else None), "(estimated)" in (spent or "")

    def inv_state_sums(self):
        try:
            st = self.state()
        except (OSError, ValueError) as e:
            yield f"state.json does not parse: {e}"
            return
        kind = {p["slug"]: p["kind"] for p in self.plans}
        want = {"work_s": 0, "maint_s": 0, "cost_usd": 0.0, "known_cost_s": 0, "estimated_runs": 0}
        n_money = 0
        for slug, ts in st.get("tasks", {}).items():
            for h in ts.get("history", []):
                bucket = "maint_s" if kind.get(slug) == "maintenance" else "work_s"
                for seconds, spent in ((h.get("seconds", 0), h.get("spent", "")),
                                       ((h.get("review") or {}).get("seconds", 0), (h.get("review") or {}).get("spent", ""))):
                    if spent is None or (seconds == 0 and not spent):
                        continue
                    want[bucket] += seconds
                    cost, est = self._money(spent)
                    if cost is not None:
                        want["cost_usd"] += cost
                        n_money += 1
                        if est:
                            want["estimated_runs"] += 1
                        else:
                            want["known_cost_s"] += seconds
        got = {k: 0 for k in want}
        for day in st.get("days", {}).values():
            for k in got:
                got[k] += day.get(k, 0)
        for k in ("work_s", "maint_s", "known_cost_s", "estimated_runs"):
            if got[k] != want[k]:
                yield f"days.{k} = {got[k]} but the history entries sum to {want[k]}"
        if abs(got["cost_usd"] - want["cost_usd"]) > 0.006 * max(1, n_money):
            yield f"days.cost_usd = {got['cost_usd']:.4f} but the history entries sum to {want['cost_usd']:.4f}"

    def inv_no_dead_locks(self):
        for lf in sorted(self.review.glob("*.lock")):
            info = R.read_lock(lf)
            status = R.lock_status(info)
            if status == "live":
                continue  # (none of ours is still running, so this would be someone else's)
            yield f"{lf.name} left behind ({status}; owner pid {(info or {}).get('pid')}, task {(info or {}).get('task')})"

    def _first_parent(self, ref: str) -> list[tuple[str, list[str], str]]:
        out = []
        for row in self.git("log", "--first-parent", "--format=%H%x09%P%x09%s", f"{self.base}..{ref}").splitlines():
            sha, parents, subject = row.split("\t", 2)
            out.append((sha, parents.split(), subject))
        return out

    def inv_master_merges_gated(self):
        st = self.state()
        for sha, parents, subject in self._first_parent("master"):
            m = re.fullmatch(r"orchestra: merge review/(\S+)", subject)
            if len(parents) < 2 or not m:
                yield f"{sha[:8]} on master is not an Orchestra merge: {subject!r}"
                continue
            ts = st.get("tasks", {}).get(m.group(1), {})
            integ = ts.get("integration") or {}
            if integ.get("commit") != sha:
                yield f"{sha[:8]} ({m.group(1)}): state's integration.commit is {str(integ.get('commit'))[:8]}, not this merge"
            checks = integ.get("checks") or []
            recovered = any(c.startswith("recovered:") for c in checks)  # tested, then the tick died before saving
            if not recovered and (not any(c.startswith("gate: pass") for c in checks) or not any(
                    c.startswith("run_checks --fast: pass") or c.startswith("run_checks --fast: skipped") for c in checks)):
                yield f"{sha[:8]} ({m.group(1)}): combined-tree checks not green: {checks}"
            if ts.get("merge") != "merged":
                yield f"{sha[:8]} ({m.group(1)}): state says merge={ts.get('merge')!r}"

    def inv_origin_gated(self):
        st = self.state()
        self.git("fetch", "-q", "origin", "master")
        r = subprocess.run(["git", "merge-base", "--is-ancestor", "origin/master", "master"], cwd=self.repo,
                           capture_output=True)
        if r.returncode:
            yield "origin/master is not an ancestor of master"
        files = self.git("log", "--format=", "--name-only", f"{self.base}..origin/master").split()
        bad = sorted({f for f in files if aftercare.PRIVATE_PATHS.search(f)})
        if bad:
            yield f"origin received private-looking paths: {bad}"
        for slug, ts in st.get("tasks", {}).items():
            push = ts.get("push") or ""
            commit = (ts.get("integration") or {}).get("commit")
            if push.startswith("pushed") and commit:
                r = subprocess.run(["git", "merge-base", "--is-ancestor", commit, "origin/master"], cwd=self.repo,
                                   capture_output=True)
                if r.returncode:
                    yield f"{slug}: state says {push!r} but {commit[:8]} is not on origin/master"

    def inv_no_orphans(self):
        for p in sorted((self.root / "grandchildren").glob("*.json")) if (self.root / "grandchildren").exists() else []:
            info = json.loads(p.read_text(encoding="utf-8"))
            if R._alive(info["pid"], info.get("created")):
                yield f"grandchild pid {info['pid']} of {info['slug']} is still running (killed now)"
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(info["pid"]), "/F"], capture_output=True)
                else:
                    try:
                        os.kill(info["pid"], 9)
                    except OSError:
                        pass

    def inv_worktrees(self):
        st = self.state()
        paths = set()
        for row in self.git("worktree", "list", "--porcelain").splitlines():
            if row.startswith("worktree ") and Path(row[9:]).resolve() != self.repo.resolve():
                paths.add(Path(row[9:]).name)
        if self.worktrees.exists():
            paths |= {p.name for p in self.worktrees.iterdir()}
        for name in sorted(paths):
            if name.endswith("--integration"):
                yield f"integration worktree left behind: {name}"
                continue
            ts = st.get("tasks", {}).get(name, {})
            if (self.review / "parked" / f"{name}.md").exists():
                continue  # parked: branch and worktree kept on purpose
            if (self.review / "queue" / f"{name}.md").exists():
                last = ts.get("last_result")
                if last in R.INFRA or last == "LIMITED":
                    continue  # awaiting its retry; fresh_worktree replaces it then
                yield f"worktree {name} for a queued task that won't run again (last_result {last!r})"
                continue
            merge = ts.get("merge", "")
            if merge and merge not in ("merged", "no commits"):
                continue  # archived as "merge by hand": kept so Chuen can
            yield f"worktree {name} left for a finished task (merge={merge!r}, last_result={ts.get('last_result')!r})"

    def inv_ticks_clean(self):
        for t in self.ticks:
            if t.get("drain_killed"):
                yield (f"tick {t['n']} was still running {TIMEOUTS['heavy'] + 90}s after the end "
                       f"(task {t.get('drain_task')}); killed by the harness")
            if t["killed"]:
                continue
            text = self._tick_output(t)
            if t["code"] not in (0, None) or "Traceback" in text:
                tail = [l for l in text.strip().splitlines() if l.strip()][-3:]
                yield f"tick {t['n']} exited {t['code']}: {' | '.join(tail)[:300]}"
        log = self.review / "runner.log"
        if log.exists():
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                if "TICK CRASHED" in line or "WATCHDOG" in line:
                    yield f"runner.log: {line[:300]}"

    def _tick_output(self, t: dict) -> str:
        try:
            return (self.root / "ticks" / f"{t['n']:04d}.out").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    # --- report
    def summary(self) -> dict:
        st = self.state() if (self.review / "state.json").exists() else {}
        tasks = st.get("tasks", {})
        slugs = {p["slug"] for p in self.plans}
        parked = {p.stem: (self.review / "parked" / f"{p.stem}.why.txt") for p in (self.review / "parked").glob("*.md")
                  if p.stem in slugs} if (self.review / "parked").exists() else {}
        reasons: dict[str, int] = {}
        for why in parked.values():
            try:
                first = why.read_text(encoding="utf-8").splitlines()[0].split(" ", 1)[-1]
            except (OSError, IndexError):
                first = "?"
            key = re.split(r"[:(]", first, 1)[0].strip()[:40]
            reasons[key] = reasons.get(key, 0) + 1
        results: dict[str, int] = {}
        for ts in tasks.values():
            for h in ts.get("history", []):
                results[h["result"]] = results.get(h["result"], 0) + 1
        return {
            "seconds": round(self.ended - self.started),
            "ticks": len(self.ticks), "ticks_killed": sum(t["killed"] for t in self.ticks),
            "lane_starts": len(self.events("START")), "reviews": len(self.events("REVIEW")),
            "merged": sum(ts.get("merge") == "merged" for ts in tasks.values()),
            "merge_by_hand": sum(bool(ts.get("merge")) and ts.get("merge") not in ("merged", "no commits") for ts in tasks.values()),
            "pushed": sum(str(ts.get("push", "")).startswith("pushed") for ts in tasks.values()),
            "archived": len(list((self.review / "archive").glob("*/*.report.md"))) if (self.review / "archive").exists() else 0,
            "parked": len(parked), "parked_reasons": reasons, "queued": self.queue_left(),
            "results": results, "master_commits": len(self._first_parent("master")),
            "origin_commits": len(self._first_parent("origin/master")),
        }

    def report(self) -> str:
        s = self.summary()
        ok = all(f["ok"] for f in self.findings)
        lines = [f"# Orchestra soak: seed {self.seed}, {self.n} tasks, {self.minutes:g} min, {self.procs} tick processes",
                 f"repo: {self.repo}", "",
                 f"ran {s['seconds']}s: {s['ticks']} ticks ({s['ticks_killed']} killed mid-run), {s['lane_starts']} lane starts, "
                 f"{s['reviews']} reviews", f"tasks: {s['merged']} merged ({s['pushed']} pushed), {s['merge_by_hand']} merge by hand, "
                 f"{s['archived']} archived, {s['parked']} parked, {s['queued']} still queued",
                 f"history results: {s['results']}", f"parked reasons: {s['parked_reasons']}",
                 f"master: {s['master_commits']} merge commits; origin: {s['origin_commits']}", "", "## Invariants"]
        for f in self.findings:
            lines.append(f"- {'OK  ' if f['ok'] else 'FAIL'} {f['name']}")
            for d in f["details"][:12]:
                lines.append(f"      {d}")
            if len(f["details"]) > 12:
                lines.append(f"      ... {len(f['details']) - 12} more")
        lines += ["", f"VERDICT: {'PASS' if ok else 'FAIL'} ({sum(not f['ok'] for f in self.findings)} invariant(s) broken)"]
        return "\n".join(lines) + "\n"

    def cleanup(self) -> None:
        for t in self.ticks:
            if t["ended"] is None:
                t["proc"].kill()
            try:
                t["out"].close()
            except OSError:
                pass
        if self.root is None:
            return
        if self.worktrees.exists():
            for wt in self.worktrees.iterdir():
                R.unlink_link(wt / ".venv")
        if self.keep:
            self.say(f"kept {self.root}")
            return
        shutil.rmtree(self.root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m orchestra.soak", description=__doc__.split("\n\n")[0])
    ap.add_argument("--minutes", type=float, default=5, help="how long to keep ticks coming (default 5)")
    ap.add_argument("--seed", type=int, default=1, help="plans, kills and power flips follow from it")
    ap.add_argument("--tasks", type=int, default=40)
    ap.add_argument("--procs", type=int, default=6, help="concurrent tick processes (default 6: every slot + 1)")
    ap.add_argument("--kill-every", type=float, default=20, help="seconds between killing a random running tick")
    ap.add_argument("--flip-every", type=float, default=15, help="seconds between fake power flips")
    ap.add_argument("--keep", action="store_true", help="keep the temp repo for a look")
    ap.add_argument("--report", type=Path, help="also write the report here")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    soak = Soak(a.minutes, a.seed, a.tasks, a.procs, a.kill_every, a.flip_every, a.keep, a.quiet)
    try:
        soak.setup()
        soak.run()
        soak.check()
        text = soak.report()
        print(text, flush=True)
        if a.report:
            a.report.parent.mkdir(parents=True, exist_ok=True)
            a.report.write_text(text, encoding="utf-8")
        return 0 if all(f["ok"] for f in soak.findings) else 1
    finally:
        soak.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
