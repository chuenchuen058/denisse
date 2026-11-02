"""Admission: the only way a machine-proposed task gets into .review/queue/.

Deterministic, no model. A packet (usually from the local team's planner) is admitted only if:
  1. it fits the packet schema, and its text can't smuggle in a header, heading or code fence
  2. every scope path is a plain relative path inside the repo, not under .review/.git/.venv,
     not a policy file (gates.json, config.json, this file, hooks, role prompts), and not in
     the vault
  3. its gate is a registered gate in orchestra/gates.json that the packet's area allows,
     with an argument that matches the gate's pattern and names a test that exists
  4. its premise holds: every evidence quote is still in the evidence file
  5. it isn't a duplicate of a queued (incl. running), parked or archived task, or of a packet
     admitted or refused earlier (same evidence fingerprint, or same normalised goal / title
     with overlapping files) - so rewording alone doesn't get a refused task back in
  6. the queue holds fewer than producer_queue_cap tasks and fewer than producer_admit_per_hour
     were admitted in the last hour
Admitted tasks are written as `Queue: draft` unless the area is in config `auto_admit_areas`.
Every decision is appended to .review/local_team/admission.jsonl; the full packet goes in a
sidecar .review/local_team/packets/<slug>.json.

    from orchestra.admission import admit
    result = admit(packet, repo)      # -> {"admitted": bool, "kind": ..., "reason": ..., "path": ...}
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
from pathlib import Path

try:
    from orchestra.local_team.core import validate
except ImportError:  # run as a plain script from orchestra/
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from orchestra.local_team.core import validate

HERE = Path(__file__).resolve().parent
DEFAULTS = {"producer_queue_cap": 20, "producer_admit_per_hour": 4, "auto_admit_areas": [],
            "producer_dedupe_days": 30, "vault_dir": ""}

PACKET_SCHEMA = {
    "type": "object",
    "required": ["title", "objective", "scope_paths", "acceptance", "gate", "weight", "evidence"],
    "properties": {
        "title": {"type": "string", "minLength": 8, "maxLength": 90},
        "objective": {"type": "string", "minLength": 20, "maxLength": 700},
        "benefit": {"type": "string", "maxLength": 300},
        "scope_paths": {"type": "array", "minItems": 1, "maxItems": 8,
                        "items": {"type": "string", "minLength": 1, "maxLength": 160}},
        "acceptance": {"type": "array", "minItems": 1, "maxItems": 4,
                       "items": {"type": "string", "minLength": 5, "maxLength": 240}},
        "gate": {"type": "object", "required": ["id", "arg"],
                 "properties": {"id": {"type": "string"}, "arg": {"type": "string"}}},
        "weight": {"type": "string", "enum": ["light", "heavy"]},
        "evidence": {"type": "array", "minItems": 1, "maxItems": 8,
                     "items": {"type": "object", "required": ["path", "quote"],
                               "properties": {"path": {"type": "string", "maxLength": 200},
                                              "quote": {"type": "string", "minLength": 8, "maxLength": 400}}}},
        "source": {"type": "string", "maxLength": 40},
        "candidate_id": {"type": "string", "maxLength": 80},
        "model": {"type": "string", "maxLength": 60},
        "roles": {"type": "object"},
    },
}

# Never in a machine-proposed task's scope: runtime data, git, the venv, and the files that
# set this layer's own policy (a log line must not be able to loosen admission).
FORBIDDEN = (".review/", ".git/", ".venv/", "venv/", "node_modules/", "models/", ".claude/",
             "orchestra/hooks/", "orchestra/local_team/roles/")
POLICY_FILES = ("orchestra/admission.py", "orchestra/gates.json", "orchestra/config.json",
                "orchestra/install_task.ps1", ".gitignore", ".gitattributes")
REMEMBERED = {"invalid", "path", "gate", "premise"}  # refusals that block the same evidence later
HEADER = re.compile(r"^[A-Za-z][A-Za-z-]*:\s")
STOP = set("a an the and or of to in on for with is are be by from at as it its this that into not "
           "no when what which so do does should can will make makes made fix fixes add adds use "
           "uses task tasks file files".split())
PATHLIKE = re.compile(r"(?<![\w/.-])((?:[\w.-]+[\\/])+[\w.-]+\.[A-Za-z0-9]+)")


def _now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def load_config(repo: Path) -> dict:
    cfg = dict(DEFAULTS)
    for path in (Path(repo) / "orchestra" / "config.json", HERE / "config.json"):
        if path.exists():
            cfg.update({k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items()
                        if not k.startswith("_")})
            break
    return cfg


def load_gates(repo: Path) -> dict:
    for path in (Path(repo) / "orchestra" / "gates.json", HERE / "gates.json"):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError("orchestra/gates.json")


# ---------------------------------------------------------------- checks

def _flat(s: str) -> str:
    return " ".join(str(s).split())


def text_problem(packet: dict) -> str | None:
    fields = [("title", packet.get("title", "")), ("objective", packet.get("objective", "")),
              ("benefit", packet.get("benefit", ""))]
    fields += [("acceptance", a) for a in packet.get("acceptance", [])]
    for name, val in fields:
        flat = _flat(val)
        if "```" in val or "~~~" in val:
            return f"{name} contains a code fence"
        if flat.startswith("#") or HEADER.match(flat):
            return f"{name} starts like a heading or header line"
    if "\n" in packet.get("title", "") or "\r" in packet.get("title", ""):
        return "title has a line break"
    return None


def path_problem(p: str, repo: Path, cfg: dict) -> str | None:
    if not isinstance(p, str) or not p.strip() or "\x00" in p:
        return "empty path"
    raw = p.strip()
    if any(c in raw for c in "~%$*?<>|\"'`") or re.match(r"^[A-Za-z]:", raw) or raw[0] in "/\\":
        return f"{raw!r} is not a plain repo-relative path"
    rel = raw.replace("\\", "/")
    parts = rel.split("/")
    if any(part in ("..", ".", "") for part in parts[:-1]) or parts[-1] in ("..", "."):
        return f"{raw!r} has '..', '.' or an empty segment"
    root = repo.resolve()
    full = (root / rel).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        return f"{raw!r} resolves outside the repository"
    vault = str(cfg.get("vault_dir") or "")
    if vault:
        try:
            full.relative_to(Path(os.path.expanduser(vault)).resolve())
            return f"{raw!r} is inside the vault"
        except ValueError:
            pass
    low = rel.lower().rstrip("/")
    if any(low == f.rstrip("/") or low.startswith(f) for f in FORBIDDEN):
        return f"{raw!r} is in a directory the local team may not scope"
    if low in POLICY_FILES:
        return f"{raw!r} is a policy file; only a human or a Claude chat changes it"
    if full.is_dir() or raw.endswith(("/", "\\")):
        # Found 2 Nov 2026: llama3.1:8b scoped a task to "orchestra/". Not a bounded task.
        return f"{raw!r} is a folder; scope names files"
    if not full.parent.is_dir():
        return f"{raw!r}: folder {Path(rel).parent.as_posix()} doesn't exist"
    return None


def area_of(rel: str, gates: dict) -> str:
    best, size = "", -1
    for name, a in gates["areas"].items():
        for pre in a["paths"]:
            if (rel == pre.rstrip("/") or rel.startswith(pre)) and len(pre) > size:
                best, size = name, len(pre)
    return best


def render_gate(gate: dict, area: str, repo: Path, gates: dict) -> tuple[str | None, str]:
    """(command, problem). The command comes from the registry template only."""
    gid, arg = str(gate.get("id", "")), str(gate.get("arg", "")).strip()
    spec = gates["gates"].get(gid)
    if not spec:
        return None, f"gate {gid!r} is not in orchestra/gates.json"
    if gid not in gates["areas"].get(area, {}).get("gates", []):
        return None, f"gate {gid!r} isn't allowed for area {area!r}"
    kind = spec.get("arg")
    if kind is None:
        if arg:
            return None, f"gate {gid!r} takes no argument"
        return spec["command"], ""
    rule = gates["args"][kind]
    arg = arg.replace("\\", "/")
    if not re.fullmatch(rule["pattern"], arg) or ".." in arg.split("/"):
        return None, f"argument {arg!r} doesn't match the {kind} pattern"
    root = repo.resolve()
    if rule.get("exists") == "module":
        if not arg.split(".")[-1].startswith("test_"):
            return None, f"{arg!r} is not a test module (last part must start with test_)"
        target = root / (arg.replace(".", "/") + ".py")
    else:
        target = root / arg
    try:
        target.resolve().relative_to(root)
    except ValueError:
        return None, f"argument {arg!r} resolves outside the repository"
    exists = target.is_dir() if rule.get("exists") == "dir" else target.is_file()
    if not exists:
        return None, f"argument {arg!r}: {target.relative_to(root).as_posix()} doesn't exist"
    shown = arg if kind == "test_module" else arg.replace("/", "\\") if kind in ("test_script", "test_dir") else arg
    return spec["command"].replace("{arg}", shown), ""


def premise_problem(evidence: list, repo: Path) -> str | None:
    root = repo.resolve()
    for ev in evidence:
        rel = str(ev.get("path", "")).replace("\\", "/")
        full = (root / rel).resolve()
        try:
            full.relative_to(root)
        except ValueError:
            return f"evidence {rel!r} is outside the repository"
        try:
            text = full.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        except OSError:
            return f"evidence file {rel!r} is gone"
        if ev["quote"].replace("\r\n", "\n") not in text:
            return f"evidence quote no longer in {rel!r}: {ev['quote'][:80]!r}"
    return None


# ---------------------------------------------------------------- identity / dedupe

def tokens(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) < 3 or w in STOP:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def norm_title(t: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", t.lower()))


def evidence_fp(evidence: list) -> str:
    keys = sorted(f"{str(e['path']).replace(chr(92), '/')}|{_flat(e['quote'])}" for e in evidence)
    return hashlib.sha1("\n".join(keys).encode("utf-8")).hexdigest()


def _overlap(a: set, b: set) -> float:
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def existing_tasks(repo: Path, days: int) -> list[dict]:
    review = repo / ".review"
    dirs = [("queued", review / "queue"), ("parked", review / "parked")]
    cutoff = (_now().date() - dt.timedelta(days=days)).isoformat()
    arch = review / "archive"
    if arch.is_dir():
        dirs += [("archived", d) for d in sorted(arch.iterdir()) if d.is_dir() and d.name >= cutoff]
    out = []
    for status, d in dirs:
        for p in sorted(d.glob("*.md")) if d.is_dir() else []:
            if p.name.endswith((".report.md", ".review.md")) or ".attempt" in p.name:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            first = text.lstrip().splitlines()[:1]
            title = first[0][2:].strip() if first and first[0].startswith("# ") else p.stem
            files = {m.replace("\\", "/") for m in PATHLIKE.findall(text)}
            out.append({"slug": p.stem, "status": status, "title": title, "files": files})
    return out


def records(repo: Path) -> list[dict]:
    path = repo / ".review" / "local_team" / "admission.jsonl"
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def duplicate_of(packet: dict, repo: Path, cfg: dict, recs: list[dict]) -> str | None:
    fp = evidence_fp(packet["evidence"])
    goal = tokens(packet["title"] + " " + packet["objective"])
    title_t, files = tokens(packet["title"]), {p.replace("\\", "/") for p in packet["scope_paths"]}
    cutoff = _now() - dt.timedelta(days=cfg["producer_dedupe_days"])
    for r in recs:
        if r.get("kind") != "admitted" and r.get("kind") not in REMEMBERED:
            continue
        try:
            if dt.datetime.fromisoformat(r["at"]) < cutoff:
                continue
        except (KeyError, ValueError):
            continue
        what = r.get("slug") or r.get("title", "?")
        if r.get("evidence_fp") == fp:
            return f"same evidence as {what} ({r['kind']} {r['at']})"
        if _jaccard(goal, set(r.get("goal", []))) >= 0.6:
            return f"same goal as {what} ({r['kind']} {r['at']})"
    for t in existing_tasks(repo, cfg["producer_dedupe_days"]):
        other = tokens(t["title"])
        if norm_title(t["title"]) == norm_title(packet["title"]):
            return f"same title as {t['status']} task {t['slug']}"
        ov = _overlap(title_t, other)
        if ov >= 0.8 and min(len(title_t), len(other)) >= 2:
            return f"same goal as {t['status']} task {t['slug']}"
        if ov >= 0.5 and files & t["files"]:
            return f"overlaps {t['status']} task {t['slug']} (goal and files {', '.join(sorted(files & t['files']))})"
    return None


# ---------------------------------------------------------------- write

class _Lock:
    def __init__(self, path: Path, wait: float = 30, stale: float = 120):
        self.path, self.wait, self.stale = path, wait, stale

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        end = time.monotonic() + self.wait
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale:
                        self.path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > end:
                    raise TimeoutError(f"{self.path} held")
                time.sleep(0.2)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def default_lane(title: str) -> str:
    try:
        from orchestra.exams.lane_for import lane_for
        return lane_for("code-edit", title)
    except Exception:  # no scores, import trouble: the old default
        return "claude"


def render_task(packet: dict, *, queue: str, lane: str, area: str, gate_cmd: str, fp: str, when: dt.datetime) -> str:
    roles = ", ".join(f"{k} v{v}" for k, v in sorted((packet.get("roles") or {}).items()))
    lines = [f"# {_flat(packet['title'])}", f"Queue: {queue}", "Priority: normal",
             f"Weight: {packet['weight']}", "Kind: work", f"Lane: {lane}",
             f"Written: {when.strftime('%Y-%m-%dT%H:%M')}",
             f"Source: {packet.get('source', 'local-team')} ({packet.get('model', '?')}; {roles or 'no roles'})",
             f"Area: {area}", f"Evidence-Fp: {fp[:16]}", "",
             "Proposed by Orchestra's local team from logged evidence and admitted by "
             "orchestra/admission.py. Check the premise first: the evidence below is what a local "
             "model read, not a verified diagnosis. If it no longer holds, say so and stop.", "",
             "## Objective", _flat(packet["objective"]), ""]
    if packet.get("benefit"):
        lines += ["Benefit: " + _flat(packet["benefit"]), ""]
    lines += ["## Evidence"]
    lines += [f"- `{e['path']}`: {_flat(e['quote'])[:300]}" for e in packet["evidence"]]
    lines += ["", "## Scope", "Touch only these unless the premise check shows otherwise:"]
    lines += [f"- `{p.replace(chr(92), '/')}`" for p in packet["scope_paths"]]
    lines += ["", "## Acceptance"] + [f"- {_flat(a)}" for a in packet["acceptance"]]
    lines += ["", "## Gate", "The registered gate for this area (orchestra/gates.json):", "", "```",
              gate_cmd, "```", ""]
    return "\n".join(lines)


def admit(packet: dict, repo: Path, *, cfg: dict | None = None, gates: dict | None = None,
          lane: str | None = None, when: dt.datetime | None = None) -> dict:
    repo = Path(repo).resolve()
    cfg = {**load_config(repo), **(cfg or {})}
    gates = gates or load_gates(repo)
    when = when or _now()
    home = repo / ".review" / "local_team"
    with _Lock(home / "admission.lock"):
        result = _decide(packet, repo, cfg, gates, lane, when)
        packet = packet if isinstance(packet, dict) else {}
        rec = {"at": when.isoformat(), "kind": result["kind"], "reason": result["reason"],
               "title": str(packet.get("title", ""))[:120], "slug": result.get("slug"),
               "candidate_id": packet.get("candidate_id")}
        if isinstance(packet.get("evidence"), list) and not validate(packet.get("evidence"), PACKET_SCHEMA["properties"]["evidence"]):
            rec["evidence_fp"] = evidence_fp(packet["evidence"])
        if isinstance(packet.get("title"), str) and isinstance(packet.get("objective"), str):
            rec["goal"] = sorted(tokens(packet["title"] + " " + packet["objective"]))
        home.mkdir(parents=True, exist_ok=True)
        with (home / "admission.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return result


def _decide(packet, repo, cfg, gates, lane, when) -> dict:
    def no(kind, reason):
        return {"admitted": False, "kind": kind, "reason": reason, "path": None}

    errs = validate(packet, PACKET_SCHEMA) if isinstance(packet, dict) else ["$: not an object"]
    if errs:
        return no("invalid", "; ".join(errs[:4]))
    bad = text_problem(packet)
    if bad:
        return no("invalid", bad)
    rels = []
    for p in packet["scope_paths"]:
        bad = path_problem(p, repo, cfg)
        if bad:
            return no("path", bad)
        rels.append(p.strip().replace("\\", "/"))
    areas = {area_of(r, gates) for r in rels}
    if len(areas) != 1 or "" in areas:
        return no("path", f"scope spans areas {sorted(areas)}; one task, one area")
    area = areas.pop()
    cmd, bad = render_gate(packet["gate"], area, repo, gates)
    if bad:
        return no("gate", bad)
    bad = premise_problem(packet["evidence"], repo)
    if bad:
        return no("premise", bad)
    dup = duplicate_of(packet, repo, cfg, records(repo))
    if dup:
        return no("duplicate", dup)
    queue_dir = repo / ".review" / "queue"
    queued = len(list(queue_dir.glob("*.md"))) if queue_dir.is_dir() else 0
    if queued >= cfg["producer_queue_cap"]:
        return no("queue-full", f"queue has {queued} tasks (cap {cfg['producer_queue_cap']})")
    hour_ago = when - dt.timedelta(hours=1)
    recent = 0
    for r in records(repo):
        try:
            recent += r.get("kind") == "admitted" and dt.datetime.fromisoformat(r["at"]) > hour_ago
        except (KeyError, ValueError):
            pass
    if recent >= cfg["producer_admit_per_hour"]:
        return no("rate", f"{recent} admitted in the last hour (limit {cfg['producer_admit_per_hour']})")

    queue = "auto" if area in (cfg.get("auto_admit_areas") or []) else "draft"
    fp = evidence_fp(packet["evidence"])
    text = render_task(packet, queue=queue, lane=lane or default_lane(packet["title"]), area=area,
                       gate_cmd=cmd, fp=fp, when=when)
    slug = re.sub(r"[^a-z0-9]+", "-", packet["title"].lower()).strip("-")[:50]
    queue_dir.mkdir(parents=True, exist_ok=True)
    stamp, n = when.strftime("%Y-%m-%dT%H%M"), 1
    while True:
        path = queue_dir / (f"{stamp}-{slug}.md" if n == 1 else f"{stamp}-{slug}-{n}.md")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # never overwrite a task
            break
        except FileExistsError:
            n += 1
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    side = repo / ".review" / "local_team" / "packets"
    side.mkdir(parents=True, exist_ok=True)
    (side / f"{path.stem}.json").write_text(json.dumps(
        {"packet": packet, "area": area, "gate_command": cmd, "queue": queue, "evidence_fp": fp,
         "goal": sorted(tokens(packet["title"] + " " + packet["objective"])), "admitted": when.isoformat(),
         "base": _head(repo)}, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"admitted": True, "kind": "admitted", "path": f".review/queue/{path.name}", "slug": path.stem,
            "queue": queue, "area": area, "gate": cmd,
            "reason": f"admitted as Queue: {queue} ({'auto_admit_areas' if queue == 'auto' else 'needs promotion'})"}


def _head(repo: Path) -> str | None:
    """The base revision, read from .git without running git."""
    try:
        git = repo / ".git"
        if git.is_file():
            git = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head[4:].strip()
            common = git.parent.parent if git.parent.name == "worktrees" else git
            for base in (git, common):
                if (base / ref).is_file():
                    return (base / ref).read_text(encoding="utf-8").strip()
            return ref
        return head
    except (OSError, IndexError):
        return None
