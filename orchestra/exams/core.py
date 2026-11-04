"""Agent exams: the bank, the graders, the scores and the router. Stdlib only.

Every exam is graded by code; no model grades another model. An agent is a callable
    agent(prompt: str, cwd: Path, timeout: float) -> Reply
so the tests can pass fake agents and the real ones live in agents.py.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .settings import load

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
BANK = HERE / load()["paths"]["bank"]
# Compatibility for callers displaying categories; execution reads the current data.
CATEGORIES = tuple(load()["categories"])


def scores_path() -> Path:
    return Path(os.environ.get("ORCHESTRA_EXAMS_SCORES") or REPO / load()["paths"]["scores"])


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


# ---------------------------------------------------------------- bank

def load_bank(category: str | None = None, bank: Path | None = None) -> list[dict]:
    bank = bank if bank is not None else HERE / load()["paths"]["bank"]
    exams = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(bank.glob("*.json"))]
    return [e for e in exams if category in (None, e["category"])]


# ---------------------------------------------------------------- agents' replies

@dataclass
class Reply:
    text: str = ""
    error: str = ""            # crash / timeout / exit code; the exam scores a fail with this reason
    unavailable: str = ""      # agent can't take work at all (not installed, IneligibleTier): skipped, not scored


def run_proc(argv: list[str], *, cwd: Path, timeout: float, stdin: str | None = None,
             env: dict | None = None) -> Reply:
    """Run one agent CLI. A timeout kills the whole process tree and becomes Reply.error."""
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **kw)
    except OSError as e:
        return Reply(unavailable=f"could not start {argv[0]}: {e}")
    try:
        out, _ = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=load()["limits"]["process_drain_s"])
        except (subprocess.TimeoutExpired, ValueError):
            pass
        return Reply(error=f"timed out after {timeout:g}s")
    if proc.returncode != 0:
        return Reply(text=out or "", error=f"exit {proc.returncode}: {_one_line(out)[-load()["limits"]["result_chars"]:]}")
    return Reply(text=out or "")


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=load()["limits"]["kill_tree_s"])
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------- graders

def _answer_line(text: str) -> str | None:
    hits = re.findall(r"^[\s*`>#-]*ANSWER\s*[:=]\s*(.+?)\s*$", text, flags=re.I | re.M)
    return hits[-1].strip("`*_ .") if hits else None


def _after_answer(text: str) -> str:
    parts = re.split(r"^[\s*`>#-]*ANSWER\s*[:=]", text, flags=re.I | re.M)
    body = parts[-1] if len(parts) > 1 else text
    body = re.split(r"^\s*FOR MEMORY", body, flags=re.I | re.M)[0]
    return body.strip().strip("`").strip()


def _json_in(text: str):
    for block in re.findall(r"```(?:json)?\s*\n(.*?)```", text, flags=re.S | re.I):
        try:
            return json.loads(block)
        except ValueError:
            pass
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            return dec.raw_decode(text[m.start():])[0]
        except ValueError:
            continue
    return None


def _code_block(text: str) -> str | None:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, flags=re.S | re.I)
    return blocks[-1] if blocks else None


def _run_test(exam: dict, cwd: Path) -> tuple[bool, str]:
    cmd = [sys.executable, *exam["grader"]["cmd"]]
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=load()["limits"]["grader_s"])
    except subprocess.TimeoutExpired:
        return False, "test timed out"
    return r.returncode == 0, _one_line(r.stderr)[-load()["limits"]["detail_chars"]:]


def grade(exam: dict, text: str, cwd: Path) -> tuple[bool, str]:
    """(passed, why). Pure code, no model."""
    g = exam["grader"]
    kind = g["type"]
    if kind == "test":
        ok, why = _run_test(exam, cwd)
        if ok:
            return True, "test passed"
        code = _code_block(text)  # agents that can't edit files reply with the whole file
        if code:
            (cwd / g["target"]).write_text(code, encoding="utf-8")
            ok, why = _run_test(exam, cwd)
            if ok:
                return True, "test passed (file from reply)"
        return False, f"test failed: {why}"
    if kind == "exact":
        got = _answer_line(text)
        want = [str(a).strip().lower() for a in g["answer"]]
        if got is None:
            return False, "no ANSWER line"
        return (got.lower() in want), f"answered {got!r}"
    if kind == "numeric":
        got = _answer_line(text)
        # the last number on the line: "4/pi = 1.2732" means 1.2732
        nums = re.findall(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", (got or "").replace(",", ""), flags=re.I)
        if not nums:
            return False, f"no number in ANSWER line ({got!r})"
        val = float(nums[-1])
        return abs(val - float(g["answer"])) <= float(g["tol"]), f"answered {val}"
    if kind == "writing":
        body = _after_answer(text)
        n = len(body.split())
        low = body.lower()
        missing = [f[0] for f in g["facts"] if not any(alt.lower() in low for alt in f)]
        if n > g["max_words"]:
            return False, f"{n} words > {g['max_words']}"
        if missing:
            return False, f"missing: {', '.join(missing)}"
        return True, f"{n} words, all facts"
    if kind == "json":
        got = _json_in(text)
        if got is None:
            return False, "no JSON object"
        return got == g["answer"], ("exact match" if got == g["answer"] else f"got {json.dumps(got)[:load()["limits"]["detail_chars"]]}")
    return False, f"unknown grader {kind!r}"


# ---------------------------------------------------------------- running

def run_exam(agent, exam: dict, timeout: float | None = None) -> dict:
    """One agent, one exam, in its own temp dir. Never raises: a crash scores a fail."""
    limits = load()["limits"]
    t = float(timeout or exam.get("timeout_s", limits["exam_s"]))
    tmp = Path(tempfile.mkdtemp(prefix=f"exam-{exam['id']}-"))
    start = time.monotonic()
    try:
        for name, content in exam.get("files", {}).items():
            (tmp / name).write_text(content, encoding="utf-8")
        try:
            reply = agent(exam["prompt"], tmp, t)
        except Exception as e:  # a broken agent is a fail with its reason, never a stopped run
            reply = Reply(error=f"{type(e).__name__}: {e}")
        secs = round(time.monotonic() - start, limits["seconds_digits"])
        if reply.unavailable:
            return {"exam": exam["id"], "skipped": reply.unavailable, "seconds": secs}
        if reply.error and not reply.text:
            return {"exam": exam["id"], "ok": False, "seconds": secs,
                    "error": reply.error[:limits["result_chars"]], "answered": False}
        try:
            ok, why = grade(exam, reply.text, tmp)
        except Exception as e:
            ok, why = False, f"grader crashed: {type(e).__name__}: {e}"
        res = {"exam": exam["id"], "ok": ok, "seconds": secs,
               "why": why[:limits["result_chars"]], "answered": True}
        if reply.error:
            res["error"] = reply.error[:limits["result_chars"]]
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(agents: dict, exams: list[dict], *, scores_file: Path | None = None, timeout: float | None = None,
        log=print) -> dict:
    """Every exam on every agent; results recorded as they come. Returns the scores."""
    path = scores_file or scores_path()
    scores = load_scores(path)
    for name, agent in agents.items():
        for exam in exams:
            res = run_exam(agent, exam, timeout)
            if "skipped" in res:
                log(f"{name:<22} {exam['id']:<14} SKIPPED  {res['skipped']}")
                # not a scored fail, but the router must not send it work until it answers again
                scores.setdefault("skipped", {})[name] = {"at": now().isoformat(timespec="seconds"),
                                                         "why": res["skipped"][:load()["limits"]["detail_chars"]]}
                save_scores(scores, path)
                break  # the agent can't take work at all; don't ask it 14 more times
            record(scores, name, exam["category"], res)
            save_scores(scores, path)
            mark = "pass" if res["ok"] else "FAIL"
            log(f"{name:<22} {exam['id']:<14} {mark:<5} {res['seconds']:>6.1f}s  {res.get('error') or res.get('why', '')}")
    return scores


# ---------------------------------------------------------------- scores

def load_scores(path: Path | None = None) -> dict:
    path = path or scores_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("agents", {})
    return data


def save_scores(scores: dict, path: Path | None = None) -> None:
    path = path or scores_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    scores["updated"] = now().isoformat(timespec="seconds")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(scores, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def record(scores: dict, agent: str, category: str, res: dict, when: dt.datetime | None = None) -> None:
    entry = {"at": (when or now()).isoformat(timespec="seconds"), **{k: v for k, v in res.items() if k != "skipped"}}
    rows = scores["agents"].setdefault(agent, {}).setdefault(category, [])
    rows.append(entry)
    del rows[:-load()["scores"]["keep"]]


def _all_rows(cats: dict) -> list[dict]:
    return sorted((r for rows in cats.values() for r in rows), key=lambda r: r["at"])


def not_working(scores: dict, at: dt.datetime | None = None) -> dict[str, str]:
    """agent -> why, using the configured failure streak and silence window."""
    data = load()
    policy, detail_chars = data["scores"], data["limits"]["detail_chars"]
    at = at or now()
    out = {}
    for agent, skip in scores.get("skipped", {}).items():
        rows = _all_rows(scores["agents"].get(agent, {}))
        if not rows or dt.datetime.fromisoformat(skip["at"]) > dt.datetime.fromisoformat(rows[-1]["at"]):
            out[agent] = f"can't take work ({skip['at'][:16]}): {skip['why']}"[:detail_chars]
    for agent, cats in scores["agents"].items():
        rows = _all_rows(cats)
        if not rows or agent in out:
            continue
        last = rows[-policy["down_after"]:]
        if len(last) == policy["down_after"] and not any(r.get("ok") for r in last):
            why = last[-1].get("error") or last[-1].get("why", "")
            out[agent] = f"last {policy['down_after']} results failed ({why})"[:detail_chars]
            continue
        answered = [r for r in rows if r.get("answered", True)]
        if not answered:
            out[agent] = "has never answered"
        elif at - dt.datetime.fromisoformat(answered[-1]["at"]) > dt.timedelta(hours=policy["silent_hours"]):
            out[agent] = f"no answer since {answered[-1]['at'][:16]}"
    return out


def stats(rows: list[dict]) -> dict:
    n = len(rows)
    passed = sum(1 for r in rows if r.get("ok"))
    return {"n": n, "passed": passed, "pass_rate": passed / n if n else 0.0,
            "median_s": statistics.median(r.get("seconds", 0) for r in rows) if n else None}


def last_pass(cats: dict) -> str | None:
    hits = [r["at"] for r in _all_rows(cats) if r.get("ok")]
    return hits[-1] if hits else None


def best(scores: dict, category: str, among: list[str] | None = None, at: dt.datetime | None = None) -> dict | None:
    """The best working agent for a category: highest pass rate, ties by speed; never one in
    'Not working'; claude only if no other agent passes. None = no measured answer."""
    down = not_working(scores, at)
    cands = []
    for agent, cats in scores["agents"].items():
        if agent in down or (among and agent not in among):
            continue
        s = stats(cats.get(category, []))
        if s["passed"]:
            cands.append({"agent": agent, "category": category, **s})
    if not cands:
        return None
    fallback_agents = load()["scores"]["fallback_agents"]
    others = [c for c in cands if c["agent"] not in fallback_agents]
    pool = others or cands
    pool.sort(key=lambda c: (-c["pass_rate"], c["median_s"], c["agent"]))
    return pool[0]


# ---------------------------------------------------------------- vault note

def vault_dir() -> Path | None:
    raw = os.environ.get("ORCHESTRA_VAULT_DIR")
    if raw is None:
        try:
            raw = json.loads((REPO / load()["paths"]["orchestra_config"]).read_text(encoding="utf-8")).get("vault_dir", "")
        except (OSError, ValueError):
            raw = ""
    if not raw:
        return None
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    return p if p.is_dir() else None


def render_note(scores: dict, at: dt.datetime | None = None) -> str:
    at = at or now()
    policy = load()
    categories = policy["categories"]
    day = at.date().isoformat()
    down = not_working(scores, at)
    agents = sorted(scores["agents"])
    lines = ["---", "name: Agent-Scores",
             "description: Generated by orchestra/exams. Pass rate and median seconds per agent and exam category; who is not working.",
             f"updated: {day}", "type: generated", "tags: [orchestra, agents]", "---", "",
             "# Agent scores", "",
             f"Generated {at.isoformat(timespec='minutes')} by `python -m orchestra.exams`. Don't edit: the next run overwrites it.",
             f"Each cell: pass rate (passed/taken) and median seconds over the last {policy['scores']['keep']} results.", "",
             "| Agent | " + " | ".join(categories) + " | Last pass |",
             "|---|" + "---|" * len(categories) + "---|"]
    for a in agents:
        cells = []
        for c in categories:
            s = stats(scores["agents"][a].get(c, []))
            cells.append(f"{s['pass_rate']:.0%} ({s['passed']}/{s['n']}), {s['median_s']:.0f}s" if s["n"] else "-")
        lp = last_pass(scores["agents"][a])
        lines.append(f"| {a} | " + " | ".join(cells) + f" | {lp[:10] if lp else 'never'} |")
    if not agents:
        lines.append("| (no results yet) |" + " |" * (len(categories) + 1))
    lines += ["", "## Best per category", ""]
    for c in categories:
        b = best(scores, c, at=at)
        lines.append(f"- {c}: " + (f"**{b['agent']}** ({b['pass_rate']:.0%}, {b['median_s']:.0f}s)" if b else "no measured answer; the fixed rules decide"))
    lines += ["", "## Not working", ""]
    lines += [f"- **{a}**: {why}" for a, why in sorted(down.items())] or ["- none"]
    return "\n".join(lines) + "\n"


def write_note(scores: dict, vault: Path | None = None, at: dt.datetime | None = None) -> Path | None:
    vault = vault or vault_dir()
    if not vault or not vault.is_dir():
        return None
    note = vault / load()["paths"]["note"]
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(render_note(scores, at), encoding="utf-8")
    return note
