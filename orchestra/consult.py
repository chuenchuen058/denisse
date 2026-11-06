"""The study tutor's opinion on a task, before its lane starts (study tutor 5/5).

Chuen (5 Nov 2026): "treat Don like a professional opinion on task". A task
whose header says `Consult: don`, or whose text names the study tooling
(consult.json path_patterns), gets the tutor's opinion on its prompt:
`python -m tutor consult --json` run in the main checkout, written into the
worktree as `.review/don.md`, named in the lane prompt, and shown to the
reviewer. Advisory, all of it: a tutor that can't answer, times out or isn't
installed is one log line and the lane runs as before; nothing here can park
a task or block a merge.

    why(fields, text)             the reason to consult, or None
    before_lane(task, wt, repo)   consult, write the note, return the prompt addition
    reviewer_section(wt)          what the reviewer is shown

Everything (the switch, patterns, command, wording) is consult.json beside
this file; ORCHESTRA_CONSULT_FILE points tests at another copy.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def data_file() -> Path:
    return Path(os.environ.get("ORCHESTRA_CONSULT_FILE") or HERE / "consult.json")


def load() -> dict:
    return json.loads(data_file().read_text(encoding="utf-8"))


def display_name(cfg: dict, repo: Path) -> str:
    """The tutor's name from its own config (tutor.json "name"), else the agent key."""
    try:
        return str(json.loads((repo / cfg["name_file"]).read_text(encoding="utf-8-sig"))[cfg["name_key"]])
    except (OSError, ValueError, KeyError, TypeError):
        return cfg["name"]


def why(fields: dict, text: str, cfg: dict | None = None) -> str | None:
    """Why this task is consulted: its header names the tutor, or its text
    names the study tooling. None when it isn't (or the switch is off)."""
    cfg = cfg or load()
    if not cfg.get("enabled"):
        return None
    if (fields.get(cfg["header"], "") or "").strip().lower() == cfg["name"].lower():
        return cfg["why_header"].format(header=cfg["header"].capitalize(), name=cfg["name"])
    for pat in cfg["path_patterns"]:
        m = re.search(pat, text, re.I | re.M)
        if m:
            return cfg["why_paths"].format(match=m.group(0).strip())
    return None


def question(title: str, text: str, cfg: dict) -> str:
    return cfg["question"].format(title=title, body=text[:cfg["question_chars"]])


def _tail(text: str, cfg: dict) -> str:
    return " ".join((text or "").split())[-cfg["tail_chars"]:]


def _python(repo: Path, cfg: dict) -> str:
    p = repo / cfg["python"]
    return str(p) if p.exists() else sys.executable


def _env() -> dict:
    # As run_lane: never a nested-claude marker, never an API key (the tutor may hand off to claude).
    return {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")
            and k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}


def ask(repo: Path, q: str, cfg: dict) -> dict:
    """Run the consult command. {ok, result, why, seconds}; never raises."""
    argv = [str(a).replace("{python}", _python(repo, cfg)).replace("{question}", q) for a in cfg["command"]]
    start = time.time()
    try:
        p = subprocess.run(argv, cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=cfg["timeout_s"], stdin=subprocess.DEVNULL, env=_env())
    except subprocess.TimeoutExpired:
        return {"ok": False, "result": None, "why": cfg["failed_timeout"].format(seconds=cfg["timeout_s"]),
                "seconds": int(time.time() - start)}
    except OSError as e:
        return {"ok": False, "result": None, "why": cfg["failed_missing"].format(exe=f"{argv[0]} ({e})"),
                "seconds": int(time.time() - start)}
    seconds = int(time.time() - start)
    out = p.stdout or ""
    result = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "answer" in data:
            result = data
            break
    if p.returncode != 0:
        tail = _tail(result.get("answer", "") if result else (p.stderr or out), cfg)
        return {"ok": False, "result": result, "why": cfg["failed_exit"].format(code=p.returncode, tail=tail),
                "seconds": seconds}
    if result is None:
        return {"ok": False, "result": None, "why": cfg["failed_no_json"].format(tail=_tail(p.stderr or out, cfg)),
                "seconds": seconds}
    return {"ok": True, "result": result, "why": "", "seconds": seconds}


def render_note(result: dict, q: str, reason: str, name: str, cfg: dict, when: str) -> str:
    sources = "\n".join(cfg["note_source"].format(link=s) for s in result.get("sources") or []) or cfg["note_none"]
    caveats = "\n".join(cfg["note_caveat"].format(text=c) for c in result.get("caveats") or []) or cfg["note_none"]
    return cfg["note"].format(name=name, when=when, why=reason, confidence=result.get("confidence", "?"),
                              who=result.get("who", "?"), answer=result.get("answer", ""), sources=sources,
                              caveats=caveats, question=q)


def before_lane(task, wt: Path, repo: Path, log=print) -> str:
    """Consult the tutor if the task wants it, write `.review/don.md` in the
    worktree, and return the paragraph to add to the lane prompt ('' when
    the task isn't consulted or the tutor couldn't answer). Never raises."""
    try:
        cfg = load()
        text = Path(task.path).read_text(encoding="utf-8", errors="replace")
        reason = why(task.fields, text, cfg)
        if not reason:
            return ""
        name = display_name(cfg, repo)
        q = question(task.title, text, cfg)
        r = ask(repo, q, cfg)
        if not r["ok"]:
            log(cfg["log_failed"].format(slug=task.slug, name=name, why=r["why"]))
            return ""
        res = r["result"]
        target = wt / cfg["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_note(res, q, reason, name, cfg, dt.datetime.now().isoformat(timespec="minutes")),
                          encoding="utf-8")
        log(cfg["log"].format(slug=task.slug, name=name, why=reason, confidence=res.get("confidence", "?"),
                              who=res.get("who", "?"), seconds=r["seconds"], file=cfg["file"]))
        return cfg["prompt_line"].format(name=name, file=cfg["file"], confidence=res.get("confidence", "?"))
    except Exception as e:  # noqa: BLE001 -- advisory: a broken consult must never stop a task
        try:
            log(f"CONSULT {getattr(task, 'slug', '?')}: skipped ({type(e).__name__}: {e})")
        except Exception:  # noqa: BLE001
            pass
        return ""


def read(wt: Path, cfg: dict | None = None) -> str | None:
    """The opinion written for this worktree, or None."""
    try:
        cfg = cfg or load()
        p = wt / cfg["file"]
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else None
    except Exception:  # noqa: BLE001
        return None


def reviewer_section(wt: Path) -> str:
    """What the reviewer prompt's {{DON}} becomes: the opinion, marked advisory, or a line saying none."""
    try:
        cfg = load()
    except Exception:  # noqa: BLE001
        return "(none)"
    text = read(wt, cfg)
    return cfg["reviewer_section"].format(text=text.strip()) if text else cfg["reviewer_none"]
