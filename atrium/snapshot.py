"""Write the hub's snapshot: small JSON tables the Atrium Hub page shows.

    .venv\\Scripts\\python.exe -m atrium snapshot [--out DIR]
Reads only local state (Orchestra's .review, the vault, a few CLI login files) and
writes agents.json, tasks.json, waiting.json, log.json, learner.json, spend.json
(default: <repo>/.review/hub). Nothing personal beyond what the vault already holds.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path

from .context import VAULT, _read, _section, _bullets

REPO = Path(__file__).resolve().parent.parent
REVIEW = REPO / ".review"
HOME = Path.home()


def _task_rows() -> list[dict]:
    try:
        state = json.loads((REVIEW / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"tasks": {}}
    rows = []

    def add(p: Path, where: str):
        text = p.read_text(encoding="utf-8", errors="replace")
        title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), p.stem)
        f = dict(re.findall(r"^(Queue|Kind|Lane|Priority):\s*(\S+)", text, re.M))
        ts = state.get("tasks", {}).get(p.stem, {})
        status = where if where != "queue" else ("draft" if f.get("Queue", "").lower() != "auto" else "queued")
        rows.append({"task": title[:90], "status": status, "priority": f.get("Priority", "normal"),
                     "lane": f.get("Lane", "claude"), "attempts": ts.get("attempts", 0),
                     "last_result": ts.get("last_result") or "", "id": p.stem})

    for p in sorted((REVIEW / "queue").glob("*.md")):
        add(p, "queue")
    for p in sorted((REVIEW / "parked").glob("*.md")):
        if not p.name.endswith(".report.md"):
            add(p, "parked")
    days = sorted((REVIEW / "archive").glob("*"))[-7:] if (REVIEW / "archive").is_dir() else []
    for d in days:
        for p in sorted(d.glob("*.md")):
            if not p.name.endswith(".report.md"):
                add(p, "done")
    return rows


def _agents() -> list[dict]:
    def exists(*parts):
        return any((HOME / p).exists() for p in parts)
    try:
        q = subprocess.run(["schtasks", "/Query", "/TN", "Denisse Orchestra", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=10)
        sched = "scheduled every 2 min (runs on mains power)" if q.returncode == 0 else "not scheduled"
    except (OSError, subprocess.SubprocessError):
        sched = "unknown"
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
        tray = "running" if "pythonw.exe" in out else "not running"
    except (OSError, subprocess.SubprocessError):
        tray = "unknown"
    return [
        {"agent": "Claude", "status": "ready", "role": "Main chat: plans, routes, reviews"},
        {"agent": "Codex", "status": "ready" if exists(".codex/auth.json") else "sign-in needed",
         "role": "Code changes, debugging, review"},
        {"agent": "Gemini", "status": "ready" if exists(".gemini/oauth_creds.json", ".gemini/google_accounts.json") else "sign-in needed",
         "role": "Long documents, research, summaries"},
        {"agent": "Copilot", "status": "ready" if any((HOME / ".copilot").glob("*token*")) or exists(".copilot/config.json") and "logged" in _read_text(HOME / ".copilot/config.json") else "sign-in needed",
         "role": "GitHub, shell commands, quick code"},
        {"agent": "Denisse", "status": tray, "role": "Local voice assistant (llama3.1:8b, Whisper, Kokoro)"},
        {"agent": "Orchestra", "status": sched, "role": "Runs queued tasks in their own worktrees"},
    ]


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return ""


def _waiting(tasks: list[dict]) -> list[dict]:
    rows = []
    for l in _bullets(_section(_read("hot.md"), "Waiting on Chuen")).splitlines():
        t = re.sub(r"^\s*[-*]\s*", "", l).strip()
        if t and t.upper() != "TODO":
            rows.append({"item": t[:160], "from": "hot.md"})
    for t in tasks:
        if t["status"] == "draft":
            rows.append({"item": f"Approve draft task: {t['task']} (change Queue: draft to auto)", "from": "Orchestra"})
        if t["status"] == "parked":
            why = REVIEW / "parked" / f"{t['id']}.why.txt"
            reason = why.read_text(encoding="utf-8").split(" ", 1)[-1].splitlines()[0] if why.exists() else "parked"
            rows.append({"item": f"Parked: {t['task']} - {reason[:120]}", "from": "Orchestra"})
    for i, r in enumerate(rows):
        r["n"] = i + 1
    return rows


def _log() -> list[dict]:
    today = dt.date.today().isoformat()
    rows = []
    for l in _read(f"Calendar/Handoff-{today}.md").splitlines():
        m = re.match(r"^\s*-\s*(\d{1,2}:\d{2})\s*\[([^\]]+)\]\s*(.*)$", l)
        if m:
            rows.append({"time": m.group(1), "agent": m.group(2), "what": m.group(3)[:220]})
    for i, r in enumerate(rows):
        r["n"] = i + 1
    return rows


def _learner() -> list[dict]:
    try:
        from learner import core
        data = core.load()
    except Exception:
        return []
    known = {b["area"] for b in data["beliefs"] if b["status"] == "current"}
    open_q = [q for q in data["questions"] if q["status"] == "open"]
    open_q.sort(key=lambda q: (q["area"] in known, q["asked_count"], q["id"]))
    return [{"id": q["id"], "area": q["area"], "question": q["text"]} for q in open_q[:4]]


def _spend() -> list[dict]:
    try:
        st = json.loads((REVIEW / "state.json").read_text(encoding="utf-8"))
        cfg = json.loads((REPO / "orchestra" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    u = st.get("days", {}).get(dt.date.today().isoformat(), {})
    return [{"day": dt.date.today().isoformat(), "cost_usd": round(u.get("cost_usd", 0), 2),
             "cap_usd": cfg.get("daily_spend_cap_usd", 0), "work_min": round(u.get("work_s", 0) / 60),
             "maint_min": round(u.get("maint_s", 0) / 60)}]


def write(out: Path | None = None) -> Path:
    out = out or REVIEW / "hub"
    out.mkdir(parents=True, exist_ok=True)
    tasks = _task_rows()
    tables = {"agents": _agents(), "tasks": tasks, "waiting": _waiting(tasks), "log": _log(),
              "learner": _learner(), "spend": _spend()}
    for name, rows in tables.items():
        (out / f"{name}.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    return out
