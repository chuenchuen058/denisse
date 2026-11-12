"""The study tutor's opinion on a task, before its lane starts (study tutor 5/5).

Chuen (5 Nov 2026): "treat Don like a professional opinion on task"; (9 Nov):
every agent goes through Don. Which tasks are consulted is the runner's config
rule `consult_applies` (config.json): all = every task, any
lane; named = a task whose header says `Consult: don` or whose text names the
study tooling (consult.json path_patterns); off = none. `consult_narrow_to`
caps that rule (shipped: named, so all means named; "" = no cap).

The consult is `python -m tutor consult --json` run in the main checkout, its
opinion written into the worktree as `.review/don.md`, named in the lane
prompt and shown to the reviewer. Quick, and never silently lost:
  * a busy GPU (the gpu.lease held by another job, or nvidia-smi at least
    consult_gpu_busy_pct busy) is waited for, up to consult_busy_wait_s;
  * the full consult holds the gpu.lease while it runs, capped at
    consult_timeout_s; the tutor's local call is schema-constrained JSON
    (tutor.json model.structured_output);
  * still busy, or the full consult failed: the notes-only path
    (`--notes-only`: no model at all, the matching lines of his notes);
  * the log line names the path used: model, notes, or none.
Advisory, all of it: a tutor that can't answer is one log line and the lane
runs as before; nothing here can park a task or block a merge.

    why(fields, text, cfg, rule)             the reason to consult, or None
    before_lane(task, wt, repo, log, rule)   consult, write the note, return the prompt addition
    reviewer_section(wt)                     what the reviewer is shown

The wording and commands are consult.json beside this file
(ORCHESTRA_CONSULT_FILE points tests at another copy); the rule and limits are
config.json (the runner passes its C; standalone, config_defaults.json then
config.json beside this file).
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
RULE_KEYS = ("consult_applies", "consult_narrow_to", "consult_timeout_s", "consult_notes_timeout_s", "consult_busy_wait_s",
             "consult_busy_poll_s", "consult_gpu_busy_pct", "consult_notes_fallback")


def data_file() -> Path:
    return Path(os.environ.get("ORCHESTRA_CONSULT_FILE") or HERE / "consult.json")


def load() -> dict:
    return json.loads(data_file().read_text(encoding="utf-8"))


def rules() -> dict:
    """The consult_* keys of config_defaults.json, then config.json, beside this file."""
    out: dict = {}
    for name in ("config_defaults.json", "config.json"):
        try:
            data = json.loads((HERE / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.update({k: v for k, v in data.items() if k in RULE_KEYS})
    return out


def display_name(cfg: dict, repo: Path) -> str:
    """The tutor's name from its own config (tutor.json "name"), else the agent key."""
    try:
        return str(json.loads((repo / cfg["name_file"]).read_text(encoding="utf-8-sig"))[cfg["name_key"]])
    except (OSError, ValueError, KeyError, TypeError):
        return cfg["name"]


def _named(fields: dict, text: str, cfg: dict) -> str | None:
    """The task names the tutor in its header, or the study tooling in its text."""
    if (fields.get(cfg["header"], "") or "").strip().lower() == cfg["name"].lower():
        return cfg["why_header"].format(header=cfg["header"].capitalize(), name=cfg["name"])
    for pat in cfg["path_patterns"]:
        m = re.search(pat, text, re.I | re.M)
        if m:
            return cfg["why_paths"].format(match=m.group(0).strip())
    return None


def why(fields: dict, text: str, cfg: dict | None = None, rule: dict | None = None) -> str | None:
    """Why this task is consulted, or None. consult_applies: all = every task (one that also
    names the tutor or the study tooling says so), named = only those, off = none. An
    unknown value counts as all. consult.json "enabled": false is off whatever the rule.
    consult_narrow_to caps it without editing it: named makes all mean named, off means off,
    anything else (the empty string) leaves consult_applies as written."""
    cfg = cfg or load()
    rule = rule if rule is not None else rules()
    applies = str(rule.get("consult_applies", "")).strip().lower()
    cap = str(rule.get("consult_narrow_to", "")).strip().lower()
    if cap == cfg["applies_off"] or (cap == cfg["applies_named"] and applies != cfg["applies_off"]):
        applies = cap
    if not cfg.get("enabled") or applies == cfg["applies_off"]:
        return None
    named = _named(fields, text, cfg)
    if named or applies == cfg["applies_named"]:
        return named
    return cfg["why_all"]


def question(title: str, text: str, cfg: dict, general: bool = False) -> str:
    """The question put to the tutor: the study one for a task about his study tooling,
    the general one (question_general) for any other task."""
    return cfg["question_general" if general else "question"].format(title=title, body=text[:cfg["question_chars"]])


def _tail(text: str, cfg: dict) -> str:
    return " ".join((text or "").split())[-cfg["tail_chars"]:]


def _python(repo: Path, cfg: dict) -> str:
    p = repo / cfg["python"]
    return str(p) if p.exists() else sys.executable


def _env() -> dict:
    # As run_lane: never a nested-claude marker, never an API key (the tutor may hand off to claude).
    return {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")
            and k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}


def ask(repo: Path, q: str, cfg: dict, command: str = "command", timeout_s: float | None = None) -> dict:
    """Run a consult command (consult.json `command`, or `notes_command`) for at most
    timeout_s (default consult.json timeout_s). {ok, result, why, seconds}; never raises."""
    timeout_s = cfg["timeout_s"] if timeout_s is None else timeout_s
    argv = [str(a).replace("{python}", _python(repo, cfg)).replace("{question}", q) for a in cfg[command]]
    start = time.time()
    try:
        p = subprocess.run(argv, cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout_s, stdin=subprocess.DEVNULL, env=_env())
    except subprocess.TimeoutExpired:
        return {"ok": False, "result": None, "why": cfg["failed_timeout"].format(seconds=int(timeout_s)),
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


# -- is the local model free? ---------------------------------------------------

def gpu_busy(repo: Path, rule: dict, cfg: dict) -> str | None:
    """Why the GPU is busy now, or None: the gpu.lease held by another job (the local team,
    an eval), or nvidia-smi at least consult_gpu_busy_pct busy (Denisse answering)."""
    try:
        from local_team.lease import GpuLease
        held = GpuLease.for_repo(repo, cfg["lease_holder"]).info()
        if held is not None:
            return cfg["busy_lease"].format(holder=held.get("holder", "?"))
    except Exception:  # noqa: BLE001 -- no lease module or file: judge by the GPU alone
        pass
    try:
        from local_team.core import gpu_utilisation
        pct = gpu_utilisation()
    except Exception:  # noqa: BLE001
        pct = None
    if pct is not None and pct >= rule["consult_gpu_busy_pct"]:
        return cfg["busy_gpu"].format(pct=pct, limit=rule["consult_gpu_busy_pct"])
    return None


def wait_free(repo: Path, rule: dict, cfg: dict, busy=None, sleep=None, clock=None) -> tuple:
    """(why it is still busy, or None; seconds waited): poll until the GPU is free or
    consult_busy_wait_s has passed. sleep/clock are looked up at call time, so a patched
    time.monotonic is the clock this uses."""
    busy = busy or (lambda: gpu_busy(repo, rule, cfg))
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    start = clock()
    while True:
        why_busy = busy()
        waited = int(clock() - start)
        if why_busy is None or waited >= rule["consult_busy_wait_s"]:
            return why_busy, waited
        sleep(max(0.0, min(rule["consult_busy_poll_s"], rule["consult_busy_wait_s"] - waited)))


def _lease(repo: Path, rule: dict, cfg: dict):
    """The gpu.lease for the full consult (ttl: its timeout): the lease, False if another
    job took it first, None if there is no lease to be had (the consult runs without)."""
    try:
        from local_team.lease import GpuLease
        lease = GpuLease.for_repo(repo, cfg["lease_holder"], ttl_s=int(rule["consult_timeout_s"]) + 1)
    except Exception:  # noqa: BLE001
        return None
    try:
        return lease if lease.try_acquire() else False
    except OSError:
        return None


def opinion(repo: Path, q: str, cfg: dict, rule: dict, busy=None, sleep=time.sleep) -> dict:
    """The tutor's answer by the quickest sound path: {ok, result, why, seconds, path, how}.
    path: model (the full consult), notes (no model: the GPU stayed busy, or the full
    consult failed) or none (neither answered). how says which and why, for log and note."""
    still, waited = wait_free(repo, rule, cfg, busy, sleep)
    first = None
    if still is None:
        lease = _lease(repo, rule, cfg)
        if lease is False:
            still = cfg["busy_lease"].format(holder=cfg["busy_someone"])
        else:
            try:
                first = ask(repo, q, cfg, "command", rule["consult_timeout_s"])
            finally:
                if lease:
                    lease.release()
            first["seconds"] += waited
            if first["ok"]:
                return {**first, "path": "model", "how": cfg["how"]["model"]}
    reason = (cfg["how"]["busy"].format(why=still, waited=waited) if still is not None
              else cfg["how"]["failed"].format(why=first["why"]))
    if not rule["consult_notes_fallback"]:
        base = first or {"ok": False, "result": None, "seconds": waited}
        return {**base, "why": reason, "path": "none", "how": reason}
    second = ask(repo, q, cfg, "notes_command", rule["consult_notes_timeout_s"])
    second["seconds"] += first["seconds"] if first else waited
    if second["ok"]:
        return {**second, "path": "notes", "how": reason}
    both = cfg["failed_both"].format(first=reason, second=second["why"])
    return {**second, "why": both, "path": "none", "how": both}


def render_note(result: dict, q: str, reason: str, name: str, cfg: dict, when: str, how: str = "") -> str:
    sources = "\n".join(cfg["note_source"].format(link=s) for s in result.get("sources") or []) or cfg["note_none"]
    caveats = "\n".join(cfg["note_caveat"].format(text=c) for c in result.get("caveats") or []) or cfg["note_none"]
    return cfg["note"].format(name=name, when=when, why=reason, confidence=result.get("confidence", "?"),
                              who=result.get("who", "?"), answer=result.get("answer", ""), sources=sources,
                              caveats=caveats, question=q, how=how or cfg["how"]["model"])


def before_lane(task, wt: Path, repo: Path, log=print, rule: dict | None = None, busy=None,
                sleep=time.sleep) -> str:
    """Consult the tutor if the config rule says so, write `.review/don.md` in the
    worktree, and return the paragraph to add to the lane prompt ('' when the task
    isn't consulted or the tutor couldn't answer). `rule`: the runner's config (its
    consult_* keys win over the files'); `busy`/`sleep` are for tests. Never raises."""
    try:
        cfg = load()
        rule = {**rules(), **{k: v for k, v in (rule or {}).items() if k in RULE_KEYS}}
        text = Path(task.path).read_text(encoding="utf-8", errors="replace")
        reason = why(task.fields, text, cfg, rule)
        if not reason:
            return ""
        if not (repo / cfg["name_file"]).exists():  # no tutor in this checkout: nothing to ask
            log(cfg["log_skipped"].format(slug=task.slug, why=cfg["not_installed"].format(file=cfg["name_file"])))
            return ""
        name = display_name(cfg, repo)
        q = question(task.title, text, cfg, general=reason == cfg["why_all"])
        r = opinion(repo, q, cfg, rule, busy, sleep)
        if not r["ok"]:
            log(cfg["log_failed"].format(slug=task.slug, name=name, why=r["why"]))
            return ""
        res = r["result"]
        target = wt / cfg["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_note(res, q, reason, name, cfg, dt.datetime.now().isoformat(timespec="minutes"),
                                      r["how"]), encoding="utf-8")
        log(cfg["log"].format(slug=task.slug, name=name, why=reason, confidence=res.get("confidence", "?"),
                              who=res.get("who", "?"), seconds=r["seconds"], file=cfg["file"], path=r["path"],
                              how=r["how"]))
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
