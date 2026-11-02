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
        {"agent": "Gemini", "status": "blocked by Google" if exists(".gemini/oauth_creds.json") else "sign-in needed",
         "role": "Free tier no longer serves Gemini CLI (2 Nov 2026); its jobs go to Codex"},
        {"agent": "Copilot", "status": "ready" if exists(".copilot/config.json") else "sign-in needed",
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


def _state() -> dict:
    try:
        return json.loads((REVIEW / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tasks": {}, "days": {}}


def _config() -> dict:
    try:
        return json.loads((REPO / "orchestra" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _running() -> list[dict]:
    """Tasks the runner's slots hold right now (from .review/runner.*.lock)."""
    rows, now = [], dt.datetime.now()
    for lf in sorted(REVIEW.glob("runner.*.lock")):
        try:
            j = json.loads(lf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not j.get("task"):
            continue
        parts = lf.stem.split(".")  # runner.claude.2
        lane = parts[1] if len(parts) > 1 else "?"
        slot = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        try:
            mins = round((now - dt.datetime.fromisoformat(j["since"])).total_seconds() / 60)
        except (KeyError, ValueError):
            mins = 0
        title = j["task"]
        q = REVIEW / "queue" / f"{j['task']}.md"
        if q.exists():
            title = next((l[2:].strip() for l in q.read_text(encoding="utf-8", errors="replace").splitlines()
                          if l.startswith("# ")), title)
        rows.append({"id": j["task"], "task": title[:90], "lane": lane, "slot": slot,
                     "since": j.get("since", "")[11:16], "minutes": mins})
    return rows


_LIMIT = re.compile(r"(usage limit|rate limit|try again at ([0-9: ]+[AP]M)|IneligibleTierError|quota)", re.I)


def _cost_num(spent: str) -> float:
    m = re.search(r"\$([0-9.]+)", spent or "")
    return float(m.group(1)) if m else 0.0


def _usage(running: list[dict]) -> list[dict]:
    """One row per model: can it take work now, and what it did today. Claude Max does not
    expose a remaining-allowance figure to the CLI, so its row shows activity, not a %."""
    st, cfg = _state(), _config()
    today = dt.date.today().isoformat()
    slots = cfg.get("lane_slots", {})
    campaign = {}
    try:
        campaign = json.loads((REVIEW / "campaign.json").read_text(encoding="utf-8"))
        if campaign.get("enabled") and campaign.get("overrides", {}).get("lane_slots"):
            slots = campaign["overrides"]["lane_slots"]
    except (OSError, ValueError):
        pass
    per = {}
    for ts in st.get("tasks", {}).values():
        for h in ts.get("history", []):
            if not str(h.get("end", "")).startswith(today):
                continue
            p = per.setdefault(h.get("lane", "?"), {"runs": 0, "pass": 0, "minutes": 0.0, "cost": 0.0, "last": ""})
            p["runs"] += 1
            p["pass"] += h.get("result") == "PASS"
            p["minutes"] += h.get("seconds", 0) / 60
            p["cost"] += _cost_num(h.get("spent", ""))
            p["last"] = max(p["last"], str(h.get("end", ""))[11:16])
    # last limit message seen in each lane's most recent logs
    limits = {}
    for log in sorted((REVIEW / "reports").glob("*.lane.log"), key=lambda p: p.stat().st_mtime)[-15:]:
        try:
            tail = log.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            continue
        slug = log.name[: -len(".lane.log")]
        last = (st.get("tasks", {}).get(slug, {}).get("history") or [{}])[-1]
        # only a run that died without a report (or the runner marked LIMITED) counts, and only
        # the end of its log: task text that merely talks about limits must not trip this
        if last.get("result") not in ("NO_REPORT", "LIMITED"):
            continue
        m = _LIMIT.search(tail[-800:])
        if m and (dt.datetime.now().timestamp() - log.stat().st_mtime) < 6 * 3600:
            lane = last.get("lane")
            if lane:
                limits[lane] = ("until " + m.group(2).strip()) if m.group(2) else m.group(1)
    # the runner's own record (orchestra: "LIMITED <slug> lane=<lane> until <time>")
    try:
        for l in (REVIEW / "runner.log").read_text(encoding="utf-8", errors="replace").splitlines()[-300:]:
            m = re.match(r"^(\S+) LIMITED \S+ lane=(\w+) until (\S+ )?(\d{1,2}:\d{2})", l)
            if m and m.group(1)[:10] == today:
                day = (m.group(3) or today).strip()
                try:
                    if dt.datetime.fromisoformat(f"{day} {m.group(4)}") <= dt.datetime.now():
                        limits.pop(m.group(2), None)  # that limit has already lifted
                        continue
                except ValueError:
                    pass
                limits[m.group(2)] = "until " + m.group(4)
    except OSError:
        pass
    busy = {}
    for r in running:
        busy[r["lane"]] = busy.get(r["lane"], 0) + 1
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=2) as resp:
            loaded = [m.get("name") for m in json.loads(resp.read()).get("models", [])]
        local = "ready, loaded: " + ", ".join(loaded) if loaded else "ready, no model loaded"
    except Exception:
        local = "Ollama not running"
    rows = []
    for lane, label, plan in (("claude", "Claude", "Max plan (allowance not exposed to the CLI)"),
                              ("codex", "Codex", "ChatGPT plan"), ("gemini", "Gemini", "free tier blocked"),
                              ("copilot", "Copilot", "GitHub Copilot"), ("local", "Local (Ollama)", "on this laptop")):
        p = per.get(lane, {"runs": 0, "pass": 0, "minutes": 0.0, "cost": 0.0, "last": ""})
        n_slots = int(slots.get(lane, 0)) if lane in ("claude", "codex", "gemini") else 0
        if lane == "local":
            status = local
        elif lane == "gemini":
            status = "blocked by Google"
        elif lane in limits:
            status = "limited " + limits[lane]
        elif busy.get(lane):
            status = f"working ({busy[lane]} of {n_slots})"
        else:
            status = "available"
        rows.append({"model": label, "lane": lane, "status": status, "plan": plan,
                     "busy": busy.get(lane, 0), "slots": n_slots, "runs_today": p["runs"],
                     "passed_today": p["pass"], "minutes_today": round(p["minutes"]),
                     "est_cost_today": round(p["cost"], 2), "last_run": p["last"],
                     "state": ("bad" if status.startswith(("limited", "blocked", "Ollama not"))
                               else "busy" if busy.get(lane) else "ok")})
    return rows


def _outputs() -> list[dict]:
    """Finished tasks of the last 3 days, newest first, with one line of what changed."""
    st = _state()
    rows = []
    cutoff = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    for slug, ts in st.get("tasks", {}).items():
        for h in ts.get("history", []):
            end = str(h.get("end", ""))
            if end[:10] < cutoff or h.get("result") in (None, "NO_REPORT"):
                continue
            report = None
            for cand in [*(REVIEW / "archive").glob(f"*/{slug}.report.md"), REVIEW / "parked" / f"{slug}.report.md",
                         REVIEW / "reports" / f"{slug}.attempt-parked.md"]:
                if cand.exists():
                    report = cand
                    break
            done, title = "", slug
            if report:
                text = report.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"^DONE:\s*\n((?:\s*-.*\n?)+)", text, re.M)
                if m:
                    done = re.sub(r"^\s*-\s*", "", m.group(1).strip().splitlines()[0])[:220]
            for cand in [*(REVIEW / "archive").glob(f"*/{slug}.md"), REVIEW / "queue" / f"{slug}.md",
                         REVIEW / "parked" / f"{slug}.md"]:
                if cand.exists():
                    title = next((l[2:].strip() for l in cand.read_text(encoding="utf-8", errors="replace")
                                  .splitlines() if l.startswith("# ")), slug)
                    break
            push = ts.get("push", "") if h.get("result") == "PASS" else ""
            rows.append({"id": f"{slug}@{end}", "when": end[5:16].replace("T", " "), "end": end,
                         "task": title[:90], "result": h.get("result"), "lane": h.get("lane", "?"),
                         "minutes": round(h.get("seconds", 0) / 60), "pushed": "yes" if push.startswith("pushed") else "no",
                         "summary": done})
    rows.sort(key=lambda r: r["end"], reverse=True)
    return rows[:40]


def _activity() -> list[dict]:
    """Minutes of agent work per day and lane, last 7 days (for the bar chart)."""
    st = _state()
    days = [(dt.date.today() - dt.timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    acc = {(d, l): 0.0 for d in days for l in ("claude", "codex")}
    for ts in st.get("tasks", {}).values():
        for h in ts.get("history", []):
            key = (str(h.get("end", ""))[:10], h.get("lane"))
            if key in acc:
                acc[key] += h.get("seconds", 0) / 60
    return [{"day": d, "lane": l, "minutes": round(acc[(d, l)])} for d in days for l in ("claude", "codex")]


def write(out: Path | None = None) -> Path:
    out = out or REVIEW / "hub"
    out.mkdir(parents=True, exist_ok=True)
    tasks = _task_rows()
    running = _running()
    tables = {"agents": _agents(), "tasks": tasks, "waiting": _waiting(tasks), "log": _log(),
              "learner": _learner(), "spend": _spend(), "running": running, "usage": _usage(running),
              "outputs": _outputs(), "activity": _activity()}
    for name, rows in tables.items():
        (out / f"{name}.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    return out
