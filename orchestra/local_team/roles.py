"""The three roles and the producer that chains them. Deterministic code around each model
call: collect a bounded input, ask once, check the answer against what was given, save.

Outputs (all under .review/local_team/, git-ignored):
  candidates/<stamp>.json   scout: evidence items + candidate problems
  packets/<slug>.json       admission sidecar for an admitted task (written by admission.py)
  plans/<stamp>-<n>.json    planner: the packet and what admission said
  memory/<slug>.json        librarian: FOR MEMORY lines with provenance (data only)
  state.json                cursors: where the last *successful* scan stopped
  producer.log              one line per decision or skip
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from . import core
from .core import BadOutput, Ctx, Skip

MAX_ITEMS = 25          # evidence items per scout call
MAX_ITEM_CHARS = 300    # each item's text (and quote) is cut to this
MAX_LOG_BYTES = 64_000  # newest part of runner.log read per scan
REPORT_CHARS = 6000     # librarian input
LOG_SIGNAL = re.compile(r"\b(PARKED|FAIL|PARTIAL|PASS-REJECTED|REVIEW-BLOCKED|INTEGRATION-REJECTED|NO_REPORT"
                        r"|TIMEOUT|Traceback|ERROR|error|broken)\b")
SECTION = re.compile(r"^(RESULT|GATE|HUMAN|PREMISE|DONE|NOT DONE|ODD|COMMITS):", re.M)


def _stamp() -> str:
    return core.now().strftime("%Y-%m-%dT%H%M%S")


def _mtime(p: Path) -> dt.datetime:
    return dt.datetime.fromtimestamp(p.stat().st_mtime).replace(microsecond=0)


def _cut(s: str) -> str:
    s = s.strip()
    return s[:MAX_ITEM_CHARS]


def report_sections(text: str) -> dict:
    """{'NOT DONE': [bullets], 'ODD': [...], ...} from a report in the runner's format."""
    out, cur = {}, None
    for line in text.replace("\r\n", "\n").splitlines():
        m = SECTION.match(line)
        if m:
            cur = m.group(1)
            rest = line[len(cur) + 1:].strip()
            out[cur] = [rest] if rest else []
        elif cur and line.strip():
            out[cur].append(line.strip())
    return out


# ---------------------------------------------------------------- scout

def collect(ctx: Ctx, cursor: dict) -> tuple[list[dict], dict]:
    """Evidence since the cursor, bounded. Returns (items, new cursor). Every item's quote is
    an exact substring of its file, so admission can re-check the premise later."""
    since = dt.datetime.fromisoformat(cursor["since"]) if cursor.get("since") else None
    items: list[dict] = []
    new = {"since": core.now().isoformat()}

    log = ctx.review / "runner.log"
    if log.exists():
        size = log.stat().st_size
        off = int(cursor.get("log_offset", 0))
        if off > size:  # rotated or truncated
            off = 0
        start = max(off, size - MAX_LOG_BYTES)
        with log.open("rb") as f:
            f.seek(start)
            chunk = f.read().decode("utf-8", errors="replace")
        if start != off:  # started mid-file: drop the partial first line
            chunk = chunk.split("\n", 1)[1] if "\n" in chunk else ""
        new["log_offset"] = size
        lines = [l.strip() for l in chunk.splitlines() if LOG_SIGNAL.search(l)]
        for l in lines[-12:]:
            items.append({"source": ctx.rel(log), "time": l[:19], "text": _cut(l)})

    def fresh(p: Path) -> bool:
        return since is None or _mtime(p) > since

    for why in sorted((ctx.review / "parked").glob("*.why.txt")):
        if fresh(why):
            first = why.read_text(encoding="utf-8", errors="replace").strip().splitlines()[:1]
            if first:
                items.append({"source": ctx.rel(why), "time": _mtime(why).isoformat(), "text": _cut(first[0])})

    for rep in sorted((ctx.review / "archive").glob("*/*.report.md")):
        if not fresh(rep):
            continue
        secs = report_sections(rep.read_text(encoding="utf-8", errors="replace"))
        n = 0
        for name in ("NOT DONE", "ODD"):
            for b in secs.get(name, []):
                text = b[2:].strip() if b.startswith("- ") else b
                if not text or text.lower().strip(".") in ("none", "nothing", "-", "n/a"):
                    continue
                if n < 6:
                    items.append({"source": ctx.rel(rep), "time": _mtime(rep).isoformat(),
                                  "section": name, "text": _cut(text)})
                    n += 1

    checks = ctx.review / "checks.json"  # scripts/run_checks.py --report .review/checks.json
    if checks.exists() and fresh(checks):
        try:
            data = json.loads(checks.read_text(encoding="utf-8"))
            for r in data.get("results", []):
                if not r.get("passed", True) and r.get("suite"):
                    items.append({"source": ctx.rel(checks), "time": _mtime(checks).isoformat(),
                                  "text": r["suite"], "note": f"suite failed, exit {r.get('exit_code')}"})
        except (OSError, ValueError, AttributeError):
            pass

    items = items[-MAX_ITEMS:]
    for i, it in enumerate(items, 1):
        it["id"] = f"E{i}"
    return items, new


def scout(ctx: Ctx) -> dict:
    """Scan the delta, ask the model for candidates, save them. The cursor moves only after
    a successful scan (a skip or bad answer leaves the evidence for next time)."""
    role = core.load_role("scout")
    st = ctx.state()
    items, cursor = collect(ctx, st.get("scout", {}))
    out = {"made": core.now().isoformat(), "role": "scout", "version": role.version,
           "evidence": items, "candidates": []}
    if not items:
        ctx.log("scout: nothing new since the last scan; waiting for new evidence (no model call)")
        st["scout"] = {**st.get("scout", {}), **cursor}
        ctx.save_state(st)
        return out
    payload = {"evidence": [{k: v for k, v in it.items() if k != "time"} for it in items]}
    ans = core.ask(ctx, role, payload)
    known = {it["id"]: it for it in items}
    for n, c in enumerate(ans["candidates"], 1):
        unknown = [e for e in c["evidence"] if e not in known]
        if unknown:
            ctx.log(f"scout: dropped a candidate citing evidence it wasn't given ({', '.join(unknown)})")
            continue
        out["candidates"].append({**c, "id": f"{out['made'][:16].replace(':', '')}-{n}",
                                  "evidence_items": [known[e] for e in c["evidence"]]})
    out["nothing_because"] = ans.get("nothing_because", "")
    d = ctx.home / "candidates"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_stamp()}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    st["scout"] = {**st.get("scout", {}), **cursor}
    ctx.save_state(st)
    ctx.log(f"scout: {len(items)} evidence items -> {len(out['candidates'])} candidates ({ctx.rel(path)})"
            + ("" if out["candidates"] else f"; nothing because: {out['nothing_because'][:150]}"))
    out["path"] = str(path)
    return out


# ---------------------------------------------------------------- planner

def _test_files(repo: Path) -> list[str]:
    found = []
    for pat in ("orchestra/**/test_*.py", "orchestra/**/test_*.mjs", "scripts/test_*.py", "eval/test_*.py",
                "atrium/**/test_*.py", "test_*.py"):
        found += [p.relative_to(repo).as_posix() for p in repo.glob(pat) if ".venv" not in p.parts]
    return sorted(set(found))[:80]


def plan(ctx: Ctx, candidate: dict, *, admit_it: bool = False, lane: str | None = None) -> dict:
    """One candidate -> one packet (validated against planner.schema.json). With admit_it,
    hand it to orchestra/admission.py, which alone writes the queue."""
    from orchestra import admission
    role = core.load_role("planner")
    gates = admission.load_gates(ctx.repo)
    payload = {
        "problem": candidate["problem"],
        "evidence": [{"source": e["source"], "text": e["text"], **({"section": e["section"]} if e.get("section") else {})}
                     for e in candidate["evidence_items"]],
        "gates": {gid: {"about": g["about"], "takes": g["arg"] or "no argument"} for gid, g in gates["gates"].items()},
        "areas": {name: {"paths": a["paths"], "gates": a["gates"]} for name, a in gates["areas"].items()},
        "test_files": _test_files(ctx.repo),
    }
    ans = core.ask(ctx, role, payload)
    packet = {**ans, "evidence": [{"path": e["source"], "quote": e["text"]} for e in candidate["evidence_items"]],
              "source": "local-team", "candidate_id": candidate.get("id", ""),
              "model": ctx.cfg["local_team_model"],
              "roles": {"scout": core.load_role("scout").version, "planner": role.version}}
    result = admission.admit(packet, ctx.repo, lane=lane) if admit_it else None
    d = ctx.home / "plans"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_stamp()}-{re.sub(r'[^a-z0-9]+', '-', ans['title'].lower())[:40].strip('-')}.json"
    path.write_text(json.dumps({"packet": packet, "admission": result}, indent=2, ensure_ascii=False), encoding="utf-8")
    ctx.log(f"planner: {ans['title']!r} -> " + (f"{result['kind']}: {result['reason']}" if result
                                                 else f"packet only ({ctx.rel(path)})"))
    return {"packet": packet, "admission": result, "path": str(path)}


def latest_candidates(ctx: Ctx) -> Path | None:
    files = sorted((ctx.home / "candidates").glob("*.json"))
    return files[-1] if files else None


# ---------------------------------------------------------------- librarian

def librarian(ctx: Ctx, report: Path) -> dict:
    """≤3 FOR MEMORY lines with provenance, saved as data. Never touches the vault."""
    role = core.load_role("librarian")
    text = report.read_text(encoding="utf-8", errors="replace")
    rel = ctx.rel(report)
    result = (re.match(r"\s*RESULT:\s*(\w+)", text) or [None, "?"])[1]
    ans = core.ask(ctx, role, {"report_path": rel, "report": text[:REPORT_CHARS]})
    lines = []
    for l in ans["lines"][:3]:
        body = " ".join(l["text"].split()).rstrip(".")
        if re.search(r"[\w.+-]+@[\w-]+\.\w+|(?i:password|api[_ ]?key|secret|token)\s*[:=]", body):
            continue  # never pass on anything that looks like a credential or address
        lines.append({"text": f"FOR MEMORY: {body}. (source: {rel}, {l['section']}, RESULT {result}; "
                              f"local-team librarian v{role.version})",
                      "section": l["section"], "provenance": rel})
    slug = report.name[:-len(".report.md")] if report.name.endswith(".report.md") else report.stem
    d = ctx.home / "memory"
    d.mkdir(parents=True, exist_ok=True)
    out = {"made": core.now().isoformat(), "report": rel, "result": result, "version": role.version,
           "model": ctx.cfg["local_team_model"], "lines": lines}
    (d / f"{slug}.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    ctx.log(f"librarian: {rel} -> {len(lines)} FOR MEMORY line(s)")
    return out


def new_reports(ctx: Ctx) -> list[Path]:
    done = set(ctx.state().get("librarian", {}).get("done", []))
    return [p for p in sorted((ctx.review / "archive").glob("*/*.report.md")) if ctx.rel(p) not in done]


def mark_librarian_done(ctx: Ctx, report: Path) -> None:
    st = ctx.state()
    lib = st.setdefault("librarian", {})
    lib["done"] = (lib.get("done", []) + [ctx.rel(report)])[-500:]
    ctx.save_state(st)


# ---------------------------------------------------------------- producer

def produce(ctx: Ctx, lane: str | None = None) -> dict:
    """One bounded pass: librarian on a few new reports, then (if the queue is short) one
    scout scan and a planner+admission for the best candidates. Skips cleanly if Ollama or
    the GPU isn't available."""
    summary = {"memory": [], "candidates": 0, "admission": []}
    core.ready(ctx)  # raises Skip
    for rep in new_reports(ctx)[: ctx.cfg["producer_librarian_per_run"]]:
        try:
            summary["memory"].append(librarian(ctx, rep)["report"])
        except BadOutput as e:
            ctx.log(f"librarian: bad answer for {ctx.rel(rep)}: {e}")
        mark_librarian_done(ctx, rep)  # one try per report; a skip (raised) leaves it for later

    queue = ctx.review / "queue"
    queued = len(list(queue.glob("*.md"))) if queue.is_dir() else 0
    if queued >= ctx.cfg["producer_target_ready"]:
        ctx.log(f"producer: queue has {queued} tasks (target {ctx.cfg['producer_target_ready']}); no scan")
        return summary
    found = scout(ctx)
    summary["candidates"] = len(found["candidates"])
    order = {"high": 0, "medium": 1, "low": 2}
    picks = sorted((c for c in found["candidates"] if c["worth"] != "low"), key=lambda c: order[c["worth"]])
    for c in picks[: ctx.cfg["producer_plans_per_run"]]:
        try:
            res = plan(ctx, c, admit_it=True, lane=lane)
        except BadOutput as e:
            ctx.log(f"planner: bad answer for {c['id']}: {e}")
            continue
        summary["admission"].append(res["admission"])
        if res["admission"]["kind"] in ("queue-full", "rate"):
            break
    return summary
