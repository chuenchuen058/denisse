"""Quality baseline for the local team: scout -> planner -> admission on hand-labelled fixtures.

    .venv\\Scripts\\python.exe -m orchestra.local_team.baseline --rounds 3
    .venv\\Scripts\\python.exe -m orchestra.local_team.baseline --only 05-failing-atrium-suite --rounds 1

Each fixture in orchestra/local_team/fixtures/<name>/ is a `review/` tree (parked why files,
archived reports and task files, a runner.log, a checks.json) copied from real Orchestra
material, plus `expected.json` written by hand:

    {"candidate": true|false, "area": "orchestra"|...|null,
     "gates": [{"id": ..., "arg": ...}, ...],   # gates that would be right for the fix
     "duplicate": true|false,                   # an existing task already covers it
     "why": "..."}

For every fixture and round the harness builds a throwaway repo (this checkout's tracked
files + the fixture as .review/), runs the real scout once, plans the best non-low
candidates (as `produce` does, at most --plans per fixture) and hands each packet to
orchestra/admission.py. Model calls go through core.ask unchanged, so they take the real
GPU lease (<main checkout>/.review/gpu.lease) and skip when the GPU is busy; a skip waits
and retries the fixture rather than scoring it.

Scores (see score()): fixture accuracy (candidate yes/no), candidate precision, admission
pass rate, duplicates caught, bad gates rejected, expected-gate rate, false admissions,
latency per role, and whether the lease was held during every sampled call. Results go to
.review/local_team/baseline/<stamp>.json; BASELINE.md is written by hand from them.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import core, roles
from .core import BadOutput, Ctx, Skip
from .lease import GpuLease

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
SRC_REPO = HERE.parent.parent
TOP_AREAS = ("orchestra", "atrium", "eval", "scripts")


# ---------------------------------------------------------------- fixtures and repos

def load_fixtures(root: Path = FIXTURES, only: list[str] | None = None) -> list[dict]:
    out = []
    for d in sorted(p for p in Path(root).iterdir() if p.is_dir() and (p / "expected.json").exists()):
        if only and d.name not in only:
            continue
        out.append({"name": d.name, "dir": d, "expected": json.loads((d / "expected.json").read_text(encoding="utf-8"))})
    return out


def tracked_files(src: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=src, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0")
            if p and not p.startswith(("orchestra/local_team/fixtures/", ".review/"))]


def build_repo(fixture: dict, dest: Path, src: Path = SRC_REPO, files: list[str] | None = None) -> Path:
    """A throwaway repo: the tracked tree (so scope paths and gate targets resolve as they
    would for real) with the fixture's review/ as .review/. The .git file points at the same
    main checkout as `src`, so the GPU lease is the real shared one."""
    dest.mkdir(parents=True)
    for rel in files if files is not None else tracked_files(src):
        s = src / rel
        if s.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(s, dest / rel)
    shutil.copytree(fixture["dir"] / "review", dest / ".review")
    cfg_path = dest / "orchestra" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    cfg["vault_dir"] = str(dest / "no-vault")       # nothing in the fixture repo is the vault
    cfg["producer_dedupe_days"] = 3650              # fixtures are dated 2 Nov 2026; keep them in reach
    cfg["auto_admit_areas"] = []                    # never auto
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    git = src / ".git"
    if git.is_file():  # a worktree: copy the pointer, so lease.main_checkout() finds the main checkout
        (dest / ".git").write_text(git.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


# ---------------------------------------------------------------- instrumented calls

class Recorder:
    """Wraps core.ask: latency per call, and the GPU lease. Two checks:
    - at the /api/chat request itself (sent and answered), the lease file must name this
      process (`lease_at_inference`);
    - sampled every 0.2 s through the call, once taken the lease must stay ours until it is
      released, with no gap (`lease_held`). Samples before the take are expected: ask()
      runs ready() first, which may re-sample nvidia-smi for 2 s."""

    def __init__(self, lease_path: Path):
        self.lease_path = lease_path
        self.calls: list[dict] = []
        self._orig = core.ask

    def _ours(self) -> bool:
        try:
            data = json.loads(self.lease_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return data.get("pid") == os.getpid() and str(data.get("holder", "")).startswith("orchestra.local_team")

    def _sample(self, stop: threading.Event, seen: list):
        while not stop.wait(0.2):
            seen.append(self._ours())

    @staticmethod
    def contiguous(seen: list) -> bool | None:
        """True if the held samples form one unbroken run (F* T+ F*)."""
        if not any(seen):
            return None
        first = seen.index(True)
        last = len(seen) - 1 - seen[::-1].index(True)
        return all(seen[first:last + 1])

    def ask(self, ctx, role, payload):
        stop, seen, at_http = threading.Event(), [], []
        t = threading.Thread(target=self._sample, args=(stop, seen), daemon=True)
        start = time.monotonic()
        t.start()
        rec = {"role": role.name}
        real_urlopen = core.urllib.request.urlopen

        def urlopen(req, *a, **kw):
            chat = getattr(req, "full_url", str(req)).endswith("/api/chat")
            if chat:
                at_http.append(self._ours())
            try:
                return real_urlopen(req, *a, **kw)
            finally:
                if chat:
                    at_http.append(self._ours())
        core.urllib.request.urlopen = urlopen
        try:
            out = self._orig(ctx, role, payload)
            rec["ok"] = True
            return out
        except Skip as e:
            rec.update(ok=False, skip=str(e))
            raise
        except BadOutput as e:
            rec.update(ok=False, bad=str(e)[:200])
            raise
        finally:
            core.urllib.request.urlopen = real_urlopen
            stop.set()
            t.join()
            rec["seconds"] = round(time.monotonic() - start, 2)
            rec["lease_samples"] = len(seen)
            rec["lease_held"] = self.contiguous(seen)
            rec["lease_at_inference"] = all(at_http) if at_http else None
            try:
                after = json.loads(self.lease_path.read_text(encoding="utf-8"))
                rec["released"] = after.get("pid") != os.getpid()
            except (OSError, ValueError):
                rec["released"] = True
            if "skip" not in rec:
                self.calls.append(rec)

    @contextlib.contextmanager
    def installed(self):
        prev, core.ask = core.ask, self.ask
        try:
            yield self
        finally:
            core.ask = prev


def wait_ready(ctx: Ctx, max_wait_s: float, poll_s: float = 30, log=print) -> None:
    """Block until core.ready() passes (Ollama up, model there, GPU not busy, no other model
    loaded) and the lease is free, or raise Skip after max_wait_s."""
    end = time.monotonic() + max_wait_s
    lease = GpuLease.for_repo(ctx.repo, "baseline-check", ctx.cfg["gpu_lease_ttl_s"])
    while True:
        try:
            core.ready(ctx)
            held = lease.info()
            if held:
                raise Skip(f"GPU lease held by {held.get('holder')}")
            return
        except Skip as e:
            if time.monotonic() >= end:
                raise
            log(f"  waiting: {e}")
            time.sleep(poll_s)


# ---------------------------------------------------------------- one fixture

def packet_area(packet: dict) -> str | None:
    from orchestra import admission
    try:
        gates = admission.load_gates(SRC_REPO)
        areas = {admission.area_of(p.strip().replace("\\", "/"), gates) for p in packet.get("scope_paths", [])}
    except Exception:
        return None
    return areas.pop() if len(areas) == 1 else None


def run_fixture(fixture: dict, repo: Path, ollama: str = "", plans: int = 2) -> dict:
    ctx = Ctx(repo, ollama=ollama)
    found = roles.scout(ctx)
    order = {"high": 0, "medium": 1, "low": 2}
    picks = sorted((c for c in found["candidates"] if c["worth"] != "low"), key=lambda c: order[c["worth"]])
    out = {"fixture": fixture["name"], "evidence_items": len(found["evidence"]),
           "candidates": [{k: c[k] for k in ("problem", "evidence", "area_hint", "worth")} for c in found["candidates"]],
           "nothing_because": found.get("nothing_because", ""), "plans": []}
    for c in picks[:plans]:
        try:
            res = roles.plan(ctx, c, admit_it=True, lane="claude")
        except BadOutput as e:
            out["plans"].append({"problem": c["problem"], "bad_output": str(e)[:300]})
            continue
        p, a = res["packet"], res["admission"]
        out["plans"].append({"problem": c["problem"], "title": p["title"], "scope_paths": p["scope_paths"],
                             "gate": p["gate"], "area": a.get("area") or packet_area(p),
                             "admission": {"kind": a["kind"], "reason": a["reason"]}})
    return out


def probe_lease(fixture: dict, repo: Path, ollama: str = "") -> dict:
    """Hold the lease as someone else would, then ask the scout: it must skip, not call."""
    ctx = Ctx(repo, ollama=ollama)
    lease = GpuLease.for_repo(repo, "baseline-probe (stands in for eval/run_eval.py)", ctx.cfg["gpu_lease_ttl_s"])
    if not lease.try_acquire():
        return {"ran": False, "why": "lease already held by someone else"}
    try:
        roles.scout(ctx)
        return {"ran": True, "respected": False, "why": "the scout called the model while the lease was held"}
    except Skip as e:
        # Any skip before the HTTP call is fine (ready() may also see the GPU busy first);
        # "Ollama call failed" would mean it called the model anyway.
        return {"ran": True, "respected": "Ollama call failed" not in str(e), "why": str(e)[:200]}
    finally:
        lease.release()


# ---------------------------------------------------------------- scoring

def _on_target(cand: dict, plan: dict | None, exp: dict) -> bool:
    if not exp.get("candidate"):
        return False
    want = exp.get("area")
    hint = (cand.get("area_hint") or "").strip().lower().strip("/")
    if plan and plan.get("area") == want:
        return True
    if want == "app":
        return hint not in TOP_AREAS
    return hint == want


def score(runs: list[dict], fixtures: list[dict]) -> dict:
    exp = {f["name"]: f["expected"] for f in fixtures}
    fx_right = cand_n = cand_ok = planned = admitted = bad_out = 0
    dup_planned = dup_caught = dup_reached = gate_refused = gate_expected = gate_valid = false_admits = 0
    ghost_scope = 0
    kinds: dict[str, int] = {}
    per_fixture: dict[str, list[str]] = {}
    for r in runs:
        e = exp[r["fixture"]]
        has = bool(r["candidates"])
        verdict = ("hit" if has else "miss") if e["candidate"] else ("false-alarm" if has else "quiet")
        per_fixture.setdefault(r["fixture"], []).append(verdict)
        fx_right += has == bool(e["candidate"])
        by_problem = {p["problem"]: p for p in r["plans"]}
        for c in r["candidates"]:
            cand_n += 1
            cand_ok += _on_target(c, by_problem.get(c["problem"]), e)
        for p in r["plans"]:
            if "bad_output" in p:
                bad_out += 1
                continue
            planned += 1
            kind = p["admission"]["kind"]
            kinds[kind] = kinds.get(kind, 0) + 1
            admitted += kind == "admitted"
            if kind == "admitted" and not e["candidate"]:
                false_admits += 1
            if kind == "admitted":  # admission checks the folder exists, not the file
                ghost_scope += any(not (SRC_REPO / sp).exists() for sp in p["scope_paths"])
            if e.get("duplicate"):
                dup_planned += 1
                dup_caught += kind == "duplicate"
                dup_reached += kind in ("duplicate", "admitted", "queue-full", "rate")  # got past path/gate/premise
            if kind == "gate":
                gate_refused += 1
            elif kind not in ("invalid", "path"):  # the gate is only checked after the scope
                gate_valid += 1
            gate_expected += {"id": p["gate"].get("id"), "arg": str(p["gate"].get("arg", "")).replace("\\", "/")} in e["gates"]
    calls = [c for r in runs for c in r.get("calls", [])]
    lat = {}
    for role in sorted({c["role"] for c in calls}):
        s = [c["seconds"] for c in calls if c["role"] == role]
        lat[role] = {"n": len(s), "median_s": round(statistics.median(s), 1), "max_s": round(max(s), 1)}
    sampled = [c for c in calls if c["lease_held"] is not None]

    def pct(a, b):
        return None if not b else round(100 * a / b)
    return {
        "fixture_runs": len(runs), "fixture_accuracy_pct": pct(fx_right, len(runs)),
        "candidates": cand_n, "candidate_precision_pct": pct(cand_ok, cand_n),
        "planned": planned, "planner_bad_output": bad_out,
        "admitted": admitted, "admission_pass_pct": pct(admitted, planned),
        "false_admissions": false_admits,
        "admission_kinds": dict(sorted(kinds.items())), "admitted_with_missing_scope_file": ghost_scope,
        "duplicate_fixture_packets": dup_planned, "duplicate_packets_reaching_dedupe": dup_reached,
        "duplicates_caught": dup_caught,
        "gate_refused": gate_refused, "gate_checked_and_passed": gate_valid,
        "gate_expected": gate_expected, "gate_expected_pct": pct(gate_expected, planned),
        "latency": lat,
        "lease_sampled_calls": len(sampled), "lease_held_all_sampled": all(c["lease_held"] for c in sampled) if sampled else None,
        "lease_held_at_every_inference": all(c.get("lease_at_inference") for c in calls) if calls else None,
        "lease_released_every_call": all(c.get("released", True) for c in calls) if calls else None,
        "per_fixture": per_fixture,
    }


# ---------------------------------------------------------------- main

def run(rounds: int = 3, only: list[str] | None = None, plans: int = 2, ollama: str = "",
        wait_gpu_s: float = 1800, out_dir: Path | None = None, log=print) -> dict:
    fixtures = load_fixtures(only=only)
    files = tracked_files(SRC_REPO)
    tmp = Path(tempfile.mkdtemp(prefix="lt-baseline-"))
    lease_path = GpuLease.for_repo(SRC_REPO, "baseline").path
    rec = Recorder(lease_path)
    runs, probes, skips = [], [], []
    started = core.now().isoformat()
    try:
        with rec.installed():
            for rnd in range(1, rounds + 1):
                for fx in fixtures:
                    give_up = time.monotonic() + wait_gpu_s
                    attempt = 0
                    while time.monotonic() < give_up:  # a skip waits and retries, within the GPU budget
                        attempt += 1
                        repo = build_repo(fx, tmp / f"r{rnd}-{fx['name']}-{attempt}", files=files)
                        ctx = Ctx(repo, ollama=ollama)
                        wait_ready(ctx, wait_gpu_s, log=log)
                        before = len(rec.calls)
                        t0 = time.monotonic()
                        try:
                            r = run_fixture(fx, repo, ollama=ollama, plans=plans)
                        except Skip as e:
                            log(f"  r{rnd} {fx['name']}: skipped mid-run ({e}); retrying")
                            del rec.calls[before:]
                            skips.append({"round": rnd, "fixture": fx["name"], "why": str(e)[:160]})
                            time.sleep(20)
                            continue
                        except BadOutput as e:  # the scout's answer didn't fit its schema
                            r = {"fixture": fx["name"], "candidates": [], "plans": [], "scout_bad_output": str(e)[:300]}
                        r.update(round=rnd, seconds=round(time.monotonic() - t0, 1), calls=rec.calls[before:])
                        runs.append(r)
                        kinds = [p.get("admission", {}).get("kind", "bad-output") for p in r["plans"]]
                        log(f"r{rnd} {fx['name']}: {len(r['candidates'])} candidate(s), plans -> {kinds or '-'} "
                            f"({r['seconds']}s)")
                        break
                    else:
                        raise Skip(f"{fx['name']}: GPU still busy after {wait_gpu_s:.0f}s of retries")
                probe_repo = build_repo(fixtures[0], tmp / f"r{rnd}-probe", files=files)
                probes.append(probe_lease(fixtures[0], probe_repo, ollama=ollama))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    result = {"started": started, "finished": core.now().isoformat(), "rounds": rounds, "plans_per_fixture": plans,
              "model": core.load_config(SRC_REPO)["local_team_model"],
              "roles": {n: core.load_role(n).version for n in ("scout", "planner")},
              "score": score(runs, fixtures), "lease_probes": probes, "skips": skips, "runs": runs}
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{core.now().strftime('%Y-%m-%dT%H%M%S')}.json"
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        result["path"] = str(path)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m orchestra.local_team.baseline")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--only", nargs="*", help="fixture names (default: all)")
    ap.add_argument("--plans", type=int, default=2, help="candidates planned per fixture (produce uses 2)")
    ap.add_argument("--wait-gpu", type=float, default=1800, help="seconds to wait for a busy GPU before giving up")
    ap.add_argument("--out", default=str(SRC_REPO / ".review" / "local_team" / "baseline"))
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    try:
        res = run(a.rounds, a.only, a.plans, wait_gpu_s=a.wait_gpu, out_dir=Path(a.out))
    except Skip as e:
        print(f"baseline: gave up: {e}")
        return 1
    print(json.dumps(res["score"], indent=2))
    print(f"lease probes: {res['lease_probes']}")
    print(f"saved: {res.get('path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
