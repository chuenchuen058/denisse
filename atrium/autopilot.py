"""Autopilot: when Orchestra's queue runs dry, write the next tasks (Chuen, 9 Nov 2026: "I want
it to be autonomous so an AI agent puts the prompt in Claude Code for me and updates it
continuously so I don't have to put it in myself").

    made = refill(cfg=..., review=..., repo=..., vault=..., state=..., available=..., power=...,
                  busy=..., files_of=..., log=...)

The runner calls it once a tick when no Queue: auto task is left (orchestra/runner.py
autopilot_step); every decision it takes is one `AUTOPILOT ...` line in runner.log.

Where the work comes from (each a switch in config autopilot_sources):
  inbox     Inbox lines in the vault's Atlas/Task Ideas.md
  not_done  NOT DONE lines of recent reports (archive and parked)
  odd       ODD lines of recent reports
  parked    parked tasks (a follow-up, unless the runner parked it waiting on Chuen)
  waiting   hot.md "Waiting on Chuen" items (not Orchestra's own block, which is generated)

What decides (the model's job, not a keyword list, AGENTS.md Rule 0): each candidate goes to
the local model (config autopilot_classifier "model") with the question in config, answered
as JSON: is it actionable, repo-only, a bug fix, a title, the files it would touch. If the
model can't answer, the fallback rule in config applies, and it never says repo-only: the
task is then a draft. Queue: auto only for repo-only work whose gate can run here; anything
else (sends as Chuen, spends, deletes, signs in, touches the vault) is Queue: draft, which
the runner lists in hot.md "Waiting on Chuen". The task body still tells the lane to stop
and put such a thing on HUMAN.

Limits, all in config: the laptop on AC (unknown counts as AC, as for dispatch); a lane with
allowance (not LIMITED, spend cap not reached for a priced lane); at most
autopilot_max_per_hour / _per_day / _per_refill new tasks; never a task whose files overlap a
queued or running task (or another new one); never anything open, parked or done in the
last autopilot_dedupe_days (same title, or the same source line already decided). Work is
spread over the lanes with allowance left: the one with the fewest recent runs gets it. A bug
fix goes to bugfix_lane whatever that says, unless that lane has no allowance.

The ledger of decisions is <review>/autopilot.json. Nothing here reads or writes the vault
except the two notes it reads; tests pass a temp vault and inject `classify` and `create`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = "autopilot.json"
FINAL = ("created", "dropped", "exists")  # statuses that block the same source line again


@dataclass
class Candidate:
    source: str            # inbox / not_done / odd / parked / waiting
    origin: str            # where it came from: a note name or a task slug
    text: str              # the line itself
    context: str = ""      # what the classifier and the task body also get
    bugfix: bool = False   # known from the source alone (a PARTIAL/FAIL follow-up, a failed park)

    @property
    def key(self) -> str:
        norm = " ".join(self.text.lower().split())
        return hashlib.sha1(f"{self.source}|{self.origin}|{norm}".encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- reading the sources

BULLET = re.compile(r"^\s*[-*+]\s+(?:\[( |x|X)\]\s+)?(.+?)\s*$")


def section(text: str, heading: str) -> str:
    """The body under a markdown heading named `heading` (any level), up to the next heading of
    the same or a higher level."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#+)\s+(.*?)\s*#*\s*$", line)
        if m and m.group(2).strip().lower() == heading.strip().lower():
            level, body = len(m.group(1)), []
            for nxt in lines[i + 1:]:
                h = re.match(r"^(#+)\s", nxt)
                if h and len(h.group(1)) <= level:
                    break
                body.append(nxt)
            return "\n".join(body)
    return ""


def bullets(text: str, empty: list[str]) -> list[str]:
    """Open bullet lines (a checked `- [x]` is done) that say something."""
    out = []
    for line in text.splitlines():
        m = BULLET.match(line)
        if not m or (m.group(1) or "").lower() == "x":
            continue
        s = m.group(2).strip()
        if s and s.strip(" .`").lower() not in empty:
            out.append(s)
    return out


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def report_sections(text: str) -> dict:
    """RESULT and the NOT DONE / ODD blocks of a session report."""
    out, key = {}, None
    first = (text.strip().splitlines() or [""])[0]
    m = re.match(r"^RESULT:\s*(\w+)", first, re.I)
    out["RESULT"] = m.group(1).upper() if m else ""
    for line in text.splitlines():
        h = re.match(r"^([A-Z][A-Z ]+):\s*(.*)$", line)
        if h:
            key = h.group(1).strip()
            out[key] = h.group(2)
        elif key:
            out[key] += "\n" + line
    return out


def from_inbox(vault: Path | None, cfg: dict) -> list[Candidate]:
    if vault is None:
        return []
    note = cfg["autopilot_inbox_note"]
    body = section(_read(vault / note), cfg["autopilot_inbox_heading"])
    return [Candidate("inbox", note, s) for s in bullets(body, cfg["autopilot_empty_bullets"])]


def recent_reports(review: Path, cfg: dict, at: dt.datetime) -> list[Path]:
    cutoff = (at.date() - dt.timedelta(days=int(cfg["autopilot_report_days"]))).isoformat()
    archive = review / "archive"
    days = [d for d in archive.glob("*") if d.is_dir() and d.name >= cutoff] if archive.is_dir() else []
    out = [p for d in sorted(days) for p in sorted(d.glob("*.report.md"))]
    parked = review / "parked"
    out += sorted(parked.glob("*.report.md")) if parked.is_dir() else []
    return out


def from_reports(review: Path, cfg: dict, at: dt.datetime) -> list[Candidate]:
    out = []
    bug_results = [r.upper() for r in cfg["autopilot_bugfix_results"]]
    for p in recent_reports(review, cfg, at):
        sec = report_sections(_read(p))
        slug = p.name[: -len(".report.md")]
        for source, name in (("not_done", "NOT DONE"), ("odd", "ODD")):
            for s in bullets(sec.get(name, ""), cfg["autopilot_empty_bullets"]):
                out.append(Candidate(source, slug, s, context=f"report {slug}: RESULT {sec['RESULT'] or '?'}",
                                     bugfix=source == "not_done" and sec["RESULT"] in bug_results))
    return out


def from_parked(review: Path, state: dict, cfg: dict) -> list[Candidate]:
    parked = review / "parked"
    out = []
    bug_results = [r.upper() for r in cfg["autopilot_bugfix_results"]]
    for why in sorted(parked.glob("*.why.txt")) if parked.is_dir() else []:
        slug = why.name[: -len(".why.txt")]
        first = (_read(why).splitlines() or [""])[0]
        reason = first.split(" ", 1)[-1].strip() if " " in first else first.strip()
        if any(re.search(p, reason, re.I) for p in cfg["autopilot_parked_skip_patterns"]):
            continue
        title = next((l[2:].strip() for l in _read(parked / f"{slug}.md").splitlines() if l.startswith("# ")), slug)
        last = str(((state.get("tasks") or {}).get(slug) or {}).get("last_result") or "").upper()
        # A session PASS that was parked after it failed the runner's gate, review or merge is a bug fix.
        bug = last in bug_results or last == "PASS"
        out.append(Candidate("parked", slug, f"{title}: {reason}", context=f"parked task {slug}, last result {last or '?'}",
                             bugfix=bug))
    return out


def from_waiting(vault: Path | None, cfg: dict) -> list[Candidate]:
    if vault is None:
        return []
    text = _read(vault / cfg["autopilot_hot_note"])
    body = section(text, cfg["autopilot_waiting_heading"])
    a, b = body.find(cfg["autopilot_waiting_skip_open"]), body.find(cfg["autopilot_waiting_skip_close"])
    if a != -1 and b > a:  # Orchestra's own generated block: the runner's items, not new work
        body = body[:a] + body[b + len(cfg["autopilot_waiting_skip_close"]):]
    note = cfg["autopilot_hot_note"]
    return [Candidate("waiting", note, s) for s in bullets(body, cfg["autopilot_empty_bullets"])]


def candidates(*, cfg, review, vault, state, at) -> list[Candidate]:
    """Every candidate from the sources switched on in config autopilot_sources, in that order."""
    want = list(cfg["autopilot_sources"])
    out, reports_read = [], False
    for src in want:
        if src == "inbox":
            out += from_inbox(vault, cfg)
        elif src in ("not_done", "odd") and not reports_read:
            reports_read = True
            out += from_reports(review, cfg, at)  # one read gives both; the filter below keeps the wanted
        elif src == "parked":
            out += from_parked(review, state, cfg)
        elif src == "waiting":
            out += from_waiting(vault, cfg)
    return [c for c in out if c.source in want]


# ---------------------------------------------------------------- deciding

def fallback_classify(c: Candidate, cfg: dict) -> dict:
    """No model answer: the config rule. Never repo-only, so whatever it keeps is a draft."""
    return {"actionable": c.source in cfg["autopilot_fallback_actionable"], "repo_only": False,
            "bugfix": c.bugfix, "title": "", "files": [], "why": "fallback rule (no model answer)"}


def model_classify(c: Candidate, cfg: dict) -> dict:
    """The local model's reading of one candidate, as JSON constrained to the config example's
    shape. Raises on any failure; the caller falls back."""
    import urllib.request
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import ollama_output
    example = cfg["autopilot_classify_example"]
    prompt = cfg["autopilot_classify_prompt"].format(source=cfg["autopilot_source_names"].get(c.source, c.source),
                                                     origin=c.origin, text=c.text, context=c.context or "-")
    import model_routing
    model = model_routing.model_for(cfg.get("autopilot_routing_job"), cfg["autopilot_model"])
    body = {"model": model, "stream": False, "format": ollama_output.schema_from_example(example),
            "messages": [{"role": "user", "content": prompt}], "options": cfg["autopilot_model_options"]}
    req = urllib.request.Request(cfg["autopilot_model_url"], data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=float(cfg["autopilot_model_timeout_s"])) as r:
        data = json.loads(r.read().decode("utf-8"))
    out = json.loads((data.get("message") or {}).get("content") or "")
    for k, v in example.items():
        if not isinstance(out.get(k), type(v)):
            raise ValueError(f"model reply: {k} is not a {type(v).__name__}")
    out["why"] = "model"
    return out


def classify_one(c: Candidate, cfg: dict, classify, log) -> dict:
    if classify is None and cfg["autopilot_classifier"] == "model":
        classify = model_classify
    if classify is None:
        return fallback_classify(c, cfg)
    try:
        out = dict(classify(c, cfg))
    except Exception as e:  # noqa: BLE001 -- a model that can't answer is the fallback rule, logged
        log(f"AUTOPILOT model could not classify {c.key} ({type(e).__name__}: {str(e)[:120]}); fallback rule")
        return fallback_classify(c, cfg)
    out.setdefault("files", [])
    out.setdefault("title", "")
    return out


def gate_runnable(gate: list[str], repo: Path) -> bool:
    """Every gate command's program exists here: a repo path (.venv\\Scripts\\python.exe) or on PATH."""
    if not gate:
        return False
    for cmd in gate:
        try:
            prog = shlex.split(cmd, posix=False)[0].strip('"')
        except (ValueError, IndexError):
            return False
        if not ((repo / prog).exists() or Path(prog).is_absolute() and Path(prog).exists() or shutil.which(prog)):
            return False
    return True


def recent_runs(state: dict, lane: str, at: dt.datetime, hours: float) -> int:
    cutoff = at - dt.timedelta(hours=hours)
    n = 0
    for ts in (state.get("tasks") or {}).values():
        for h in ts.get("history") or []:
            try:
                end = dt.datetime.fromisoformat(h.get("end", ""))
            except (TypeError, ValueError):
                continue
            if h.get("lane") == lane and end >= cutoff:
                n += 1
    return n


def choose_lane(bugfix: bool, available: dict, load: dict, cfg: dict) -> tuple[str | None, str]:
    """(lane, why). Bug fixes to bugfix_lane if it has allowance; otherwise the lane with
    allowance left and the fewest recent runs (config order breaks a tie)."""
    order = list(cfg["autopilot_lanes"])
    free = [l for l in order if l in available and available[l] is None]
    if not free:
        return None, "no lane has allowance left: " + "; ".join(f"{l} {w}" for l, w in available.items() if w)
    want = cfg["bugfix_lane"]
    if bugfix and want in free:
        return want, cfg["bugfix_route_note"].format(lane=want)
    lane = min(free, key=lambda l: (load.get(l, 0), order.index(l)))
    why = "fewest recent runs (" + ", ".join(f"{l} {load.get(l, 0)}" for l in free) + ")"
    if bugfix and want:
        why += f"; a bug fix, but {want} is {available.get(want) or 'not a planner lane'}"
    return lane, why


def make_title(c: Candidate, cls: dict, cfg: dict) -> str:
    raw = " ".join(str(cls.get("title") or "").split()) or " ".join(c.text.split())
    raw = re.sub(r"[`*_\[\]]+", "", raw).strip(" .:-")
    n = int(cfg["autopilot_title_max"])
    raw = raw if len(raw) <= n else raw[: n - 1].rstrip() + "…"
    return cfg["autopilot_titles"].get(c.source, "{text}").format(text=raw)


def make_body(c: Candidate, cfg: dict, why: str) -> str:
    return cfg["autopilot_body"].format(text=c.text, source=cfg["autopilot_source_names"].get(c.source, c.source),
                                        origin=c.origin, context=c.context or "-", why=why)


# ---------------------------------------------------------------- ledger

def load_ledger(review: Path) -> dict:
    try:
        data = json.loads((review / LEDGER).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"entries": [], "last_skip": ""}


def save_ledger(review: Path, ledger: dict, at: dt.datetime, keep_days: int) -> None:
    cutoff = (at - dt.timedelta(days=keep_days)).isoformat()
    ledger["entries"] = [e for e in ledger["entries"] if str(e.get("at", "")) >= cutoff]
    review.mkdir(parents=True, exist_ok=True)
    tmp = review / (LEDGER + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    os.replace(tmp, review / LEDGER)


def _since(entries: list, at: dt.datetime, delta: dt.timedelta, status: str) -> int:
    cutoff = (at - delta).isoformat()
    return sum(1 for e in entries if e.get("status") == status and str(e.get("at", "")) >= cutoff)


# ---------------------------------------------------------------- the planner

def refill(*, cfg: dict, review: Path, repo: Path, vault: Path | None, state: dict, available: dict, power: dict,
           busy: dict, files_of, log, classify=None, create=None, at: dt.datetime | None = None) -> list[Path]:
    """Write up to the caps' worth of new tasks into <review>/queue. Returns their paths."""
    at = at or dt.datetime.now().replace(microsecond=0)
    review, repo = Path(review), Path(repo)
    if not cfg["autopilot"]:
        return []
    ledger = load_ledger(review)
    entries = ledger["entries"]

    def skip(why: str) -> list:
        if ledger.get("last_skip") != why:  # one line when the reason changes, not one per tick
            log(f"AUTOPILOT skip: {why}")
            ledger["last_skip"] = why
            save_ledger(review, ledger, at, int(cfg["autopilot_dedupe_days"]))
        return []

    if power.get("ac") is False:
        return skip("on battery")
    hour = int(cfg["autopilot_max_per_hour"]) - _since(entries, at, dt.timedelta(hours=1), "created")
    day = int(cfg["autopilot_max_per_day"]) - _since(entries, at, dt.timedelta(days=1), "created")
    room = min(hour, day, int(cfg["autopilot_max_per_refill"]))
    if room <= 0:
        return skip(f"cap reached ({'hour' if hour <= 0 else 'day' if day <= 0 else 'per refill'}: "
                    f"{cfg['autopilot_max_per_hour']}/hour, {cfg['autopilot_max_per_day']}/day)")
    if not any(available.get(l) is None for l in cfg["autopilot_lanes"] if l in available):
        return skip(choose_lane(False, available, {}, cfg)[1])

    window = dt.timedelta(days=int(cfg["autopilot_dedupe_days"]))
    decided = {e["key"]: e for e in entries if e.get("status") in FINAL
               and str(e.get("at", "")) >= (at - window).isoformat()}
    held = {e["key"]: e for e in entries if e.get("status") == "held"}
    held_before = {k: e.get("why") for k, e in held.items()}
    cands = [c for c in candidates(cfg=cfg, review=review, vault=vault, state=state, at=at) if c.key not in decided]
    if not cands:
        return skip("nothing new in the sources")
    ledger["last_skip"] = ""

    if create is None:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from orchestra import new_task
        create, same_title = new_task.create, new_task.same_title
    else:
        same_title = getattr(create, "same_title", lambda _t, _r: [])

    load = {l: recent_runs(state, l, at, float(cfg["autopilot_balance_hours"])) for l in cfg["autopilot_lanes"]}
    gate = list(cfg["autopilot_gate"])
    can_gate = gate_runnable(gate, repo)
    taken: dict = {k: set(v) for k, v in busy.items()}
    made = []

    def note(c: Candidate, status: str, why: str, **extra) -> None:
        entries[:] = [e for e in entries if not (e.get("key") == c.key and e.get("status") == "held")]
        entries.append({"at": at.isoformat(), "key": c.key, "status": status, "source": c.source,
                        "origin": c.origin, "text": c.text[:200], "why": why, **extra})

    for c in cands:
        if len(made) >= room:
            break
        # A held candidate keeps its reading: the model is asked once, not every tick.
        cached = (held.get(c.key) or {}).get("cls")
        cls = dict(cached) if isinstance(cached, dict) else classify_one(c, cfg, classify, log)
        bug = bool(cls.get("bugfix")) or c.bugfix
        if not cls.get("actionable"):
            note(c, "dropped", f"not actionable ({cls.get('why', '?')})")
            log(f"AUTOPILOT dropped {c.source} {c.key}: not actionable ({cls.get('why', '?')}): {c.text[:100]}")
            continue
        title = make_title(c, cls, cfg)
        same = same_title(title, review)
        if same:
            note(c, "exists", f"same title as {Path(same[0]).name}")
            log(f"AUTOPILOT not re-created {title!r}: open, parked or done recently ({Path(same[0]).name})")
            continue
        files = {str(f).replace("\\", "/") for f in cls.get("files") or [] if str(f).strip()}
        try:
            files |= set(files_of(f"{c.text}\n{c.context}"))
        except Exception:  # noqa: BLE001
            pass
        clash = next(((slug, sorted(files & theirs)) for slug, theirs in sorted(taken.items()) if files & theirs), None)
        if clash:
            why = f"overlaps {clash[0]} on {', '.join(clash[1][:5])}"
            if held_before.get(c.key) != why:
                log(f"AUTOPILOT held {title!r}: {why}")
            note(c, "held", why, cls=cls)
            continue
        lane, lane_why = choose_lane(bug, available, load, cfg)
        if lane is None:
            log(f"AUTOPILOT skip: {lane_why}")
            break
        auto = bool(cls.get("repo_only")) and can_gate
        why_queue = ("auto: repo-only, gate runnable" if auto else
                     "draft: " + ("gate not runnable here" if cls.get("repo_only") else
                                  "not shown to be repo-only (may send, spend, delete, sign in or touch the vault)"))
        kind = cfg["bugfix_kinds"][0] if bug and cfg["bugfix_kinds"] else "work"
        path = create(title, body=make_body(c, cfg, f"{lane_why}; {why_queue}"), gate=gate, kind=kind, lane=lane,
                      auto=auto, source=cfg["autopilot_source"], review=review, weight=cfg["autopilot_weight"])
        made.append(Path(path))
        load[lane] = load.get(lane, 0) + 1
        taken[Path(path).stem] = files
        note(c, "created", why_queue, slug=Path(path).stem, lane=lane, auto=auto)
        log(f"AUTOPILOT created {Path(path).name} lane={lane} ({lane_why}) {why_queue}; from {c.source} {c.origin}")
    save_ledger(review, ledger, at, int(cfg["autopilot_dedupe_days"]))
    return made
