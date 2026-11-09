"""Write the hub's snapshot: small JSON tables the Atrium Hub page shows.

    .venv\\Scripts\\python.exe -m atrium snapshot [--out DIR]
Reads only local state (Orchestra's .review, the vault, a few CLI login files) and
writes eleven tables (agents, tasks, waiting, log, learner, spend, running, usage,
outputs, activity, study; default folder <repo>/.review/hub). Nothing personal beyond what
the vault already holds. Every limit, path, label and sentence lives in
atrium/config.json ("snapshot"), read at call time.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import conf, handoff
from .context import _read, _section, _bullets

REPO = Path(__file__).resolve().parent.parent
REVIEW = REPO / conf.section("snapshot")["paths"]["review"]
HOME = Path.home()


def _c() -> dict:
    return conf.section("snapshot")


def _title(p: Path, default: str) -> str:
    """A task file's "# " heading (the task file format), else default."""
    return next((l[2:].strip() for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
                 if l.startswith("# ")), default)


def _denisse_models() -> str:
    """The chat and Whisper models Denisse is configured to use (config.json, with the
    DENISSE_MODEL / DENISSE_WHISPER_MODEL overrides), never a guessed name."""
    c = _c()
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    try:
        import model_config
        return c["models"].format(chat=model_config.model('chat'), whisper=model_config.model('whisper'))
    except (ImportError, OSError, ValueError, KeyError) as exc:
        return c["models_unreadable"].format(error=type(exc).__name__)


def _task_rows() -> list[dict]:
    c = _c()
    n, dflt = c["title_chars"], c["task_defaults"]
    try:
        state = json.loads((REVIEW / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"tasks": {}}
    rows = []

    def add(p: Path, where: str):
        text = p.read_text(encoding="utf-8", errors="replace")
        title = _title(p, p.stem)
        f = dict(re.findall(r"^(Queue|Kind|Lane|Priority):\s*(\S+)", text, re.M))
        ts = state.get("tasks", {}).get(p.stem, {})
        status = where if where != "queue" else ("draft" if f.get("Queue", "").lower() != "auto" else "queued")
        rows.append({"task": title[:n], "status": status, "priority": f.get("Priority", dflt["priority"]),
                     "lane": f.get("Lane", dflt["lane"]), "attempts": ts.get("attempts", 0),
                     "last_result": ts.get("last_result") or "", "id": p.stem})

    for p in sorted((REVIEW / "queue").glob("*.md")):
        add(p, "queue")
    for p in sorted((REVIEW / "parked").glob("*.md")):
        if not p.name.endswith(".report.md"):
            add(p, "parked")
    days = sorted((REVIEW / "archive").glob("*"))[-c["archive_days"]:] if (REVIEW / "archive").is_dir() else []
    for d in days:
        for p in sorted(d.glob("*.md")):
            if not p.name.endswith(".report.md"):
                add(p, "done")
    return rows


def _probe(spec: dict, timeout: float, unknown: str) -> str:
    """Run spec["command"]: spec["yes"] if it worked (and its output names spec["image"],
    when one is given), else spec["no"]; `unknown` if it couldn't run at all."""
    try:
        q = subprocess.run(spec["command"], capture_output=True, text=True, timeout=timeout)
        ok = spec["image"] in q.stdout if spec.get("image") else q.returncode == 0
        return spec["yes"] if ok else spec["no"]
    except (OSError, subprocess.SubprocessError):
        return unknown


def _agents() -> list[dict]:
    c = _c()
    live = {"schedule": _probe(c["schedule"], c["probe_timeout_s"], c["unknown"]),
            "tray": _probe(c["tray"], c["probe_timeout_s"], c["unknown"])}
    rows = []
    for a in c["agents"]:
        status = a["status"].format(**live)
        if a.get("login_file") and not (HOME / a["login_file"]).exists():
            status = a["missing"]
        role = a["role"].format(models=_denisse_models()) if "{models}" in a["role"] else a["role"]
        rows.append({"agent": a["agent"], "status": status, "role": role})
    return rows


def waiting_items(tasks: list[dict]) -> list[dict]:
    """What waits on Chuen, each tagged with its kind ("note" = the vault's list, "draft",
    "parked"): the hub's waiting table without the tag, and the live page's Needs you."""
    w = _c()["waiting"]
    skip = {s.upper() for s in w["skip"]}
    rows = []
    for l in _bullets(_section(_read(w["from"]), w["section"])).splitlines():
        t = re.sub(r"^\s*[-*]\s*", "", l).strip()
        if t and t.upper() not in skip:
            rows.append({"item": t[:w["item_chars"]], "from": w["from"], "kind": "note"})
    for t in tasks:
        if t["status"] == "draft":
            rows.append({"item": w["draft"].format(task=t["task"]), "from": w["orchestra"], "kind": "draft"})
        if t["status"] == "parked":
            why = REVIEW / "parked" / f"{t['id']}.why.txt"
            reason = (why.read_text(encoding="utf-8").split(" ", 1)[-1].splitlines()[0] if why.exists()
                      else w["parked_default"])
            rows.append({"item": w["parked"].format(task=t["task"], reason=reason[:w["reason_chars"]]),
                         "from": w["orchestra"], "kind": "parked", "task": t["task"], "reason": reason})
    return rows


def _waiting(tasks: list[dict]) -> list[dict]:
    rows = [{"item": r["item"], "from": r["from"]} for r in waiting_items(tasks)]
    for i, r in enumerate(rows):
        r["n"] = i + 1
    return rows


def _log() -> list[dict]:
    n = _c()["log_chars"]
    try:
        text = handoff.path().read_text(encoding="utf-8")
    except OSError:
        text = ""
    rows = []
    for l in text.splitlines():  # the handoff line format, "- HH:MM [agent] text"
        m = re.match(r"^\s*-\s*(\d{1,2}:\d{2})\s*\[([^\]]+)\]\s*(.*)$", l)
        if m:
            rows.append({"time": m.group(1), "agent": m.group(2), "what": m.group(3)[:n]})
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
    return [{"id": q["id"], "area": q["area"], "question": q["text"]} for q in open_q[:_c()["learner_questions"]]]


def _spend() -> list[dict]:
    try:
        st = json.loads((REVIEW / "state.json").read_text(encoding="utf-8"))
        cfg = json.loads((REPO / _c()["paths"]["orchestra_config"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    u = st.get("days", {}).get(dt.date.today().isoformat(), {})
    cap = cfg.get("daily_spend_cap_usd", 0)
    try:  # a live campaign's override wins, as in the runner (3 Nov: the hub showed $43 of $5)
        c = json.loads((REVIEW / "campaign.json").read_text(encoding="utf-8"))
        stop = c.get("stop_at")
        if c.get("enabled") and (not stop or dt.datetime.fromisoformat(stop.replace("Z", "+00:00"))
                                 > dt.datetime.now(dt.timezone.utc)):
            cap = (c.get("overrides") or {}).get("daily_spend_cap_usd", cap)
    except (OSError, ValueError, TypeError):
        pass
    return [{"day": dt.date.today().isoformat(), "cost_usd": round(u.get("cost_usd", 0), 2),
             "cap_usd": cap, "work_min": round(u.get("work_s", 0) / 60),
             "maint_min": round(u.get("maint_s", 0) / 60)}]


def _state() -> dict:
    try:
        return json.loads((REVIEW / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tasks": {}, "days": {}}


def _config() -> dict:
    try:
        return json.loads((REPO / _c()["paths"]["orchestra_config"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _slots() -> dict:
    """Lane slots in force now: an enabled, unexpired campaign's override, else config.json."""
    slots = _config().get("lane_slots") or dict(_c()["default_slots"])
    try:
        c = json.loads((REVIEW / "campaign.json").read_text(encoding="utf-8"))
        stop = c.get("stop_at")
        live = c.get("enabled") and (not stop or dt.datetime.fromisoformat(stop.replace("Z", "+00:00"))
                                     > dt.datetime.now(dt.timezone.utc))
        if live and (c.get("overrides") or {}).get("lane_slots"):
            slots = c["overrides"]["lane_slots"]
    except (OSError, ValueError, TypeError):
        pass
    return slots


def _running() -> list[dict]:
    """Tasks the runner's slots hold right now (from .review/runner.*.lock)."""
    n = _c()["title_chars"]
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
            title = _title(q, title)
        rows.append({"id": j["task"], "task": title[:n], "lane": lane, "slot": slot,
                     "since": j.get("since", "")[11:16], "minutes": mins})
    # One row per configured slot, idle ones included: the hub can't read an empty file
    # (3 Nov 2026: "Tasks running now" showed "failed" whenever nothing ran).
    taken = {(r["lane"], r["slot"]) for r in rows}
    for lane, k in _slots().items():
        for slot in range(1, int(k) + 1):
            if (lane, slot) not in taken:
                rows.append({"id": f"idle:{lane}:{slot}", "task": "", "lane": lane, "slot": slot,
                             "since": "", "minutes": 0})
    rows.sort(key=lambda r: (r["task"] == "", r["lane"], r["slot"]))
    return rows


def _cost_num(spent: str) -> float:
    m = re.search(r"\$([0-9.]+)", spent or "")
    return float(m.group(1)) if m else 0.0


def _exam_pass_rates() -> dict[str, float]:
    """lane -> pass rate over the results kept in .review/exams/scores.json (orchestra/exams),
    all categories together; local:<model> rows count for "local". Absent = no exam result."""
    try:
        agents = json.loads((REVIEW / "exams" / "scores.json").read_text(encoding="utf-8")).get("agents", {})
    except (OSError, ValueError, AttributeError):
        return {}
    tally: dict[str, list[int]] = {}
    for name, cats in (agents or {}).items():
        lane = name.split(":", 1)[0]
        t = tally.setdefault(lane, [0, 0])
        for rows in (cats or {}).values():
            for r in rows or []:
                t[0] += bool(r.get("ok"))
                t[1] += 1
    return {lane: round(p / n, 3) for lane, (p, n) in tally.items() if n}


def _usage(running: list[dict]) -> list[dict]:
    """One row per model: can it take work now, and what it did today. Claude Max does not
    expose a remaining-allowance figure to the CLI, so its row shows activity, not a %."""
    c = _c()
    words = c["usage_status"]
    limit_re = re.compile(c["limit_pattern"], re.I)
    st = _state()
    today = dt.date.today().isoformat()
    slots = _slots()
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
    for log in sorted((REVIEW / "reports").glob("*.lane.log"), key=lambda p: p.stat().st_mtime)[-c["lane_logs"]:]:
        try:
            tail = log.read_text(encoding="utf-8", errors="replace")[-c["lane_log_chars"]:]
        except OSError:
            continue
        slug = log.name[: -len(".lane.log")]
        last = (st.get("tasks", {}).get(slug, {}).get("history") or [{}])[-1]
        # only a run that died without a report (or the runner marked LIMITED) counts, and only
        # the end of its log: task text that merely talks about limits must not trip this
        if last.get("result") not in c["limit_results"]:
            continue
        m = limit_re.search(tail[-c["lane_log_tail_chars"]:])
        if m and (dt.datetime.now().timestamp() - log.stat().st_mtime) < c["limit_fresh_hours"] * 3600:
            lane = last.get("lane")
            if lane:
                limits[lane] = words["until"].format(time=m.group(2).strip()) if m.group(2) else m.group(1)
    # the runner's own record (orchestra: "LIMITED <slug> lane=<lane> until <time>")
    try:
        lines = (REVIEW / "runner.log").read_text(encoding="utf-8", errors="replace").splitlines()
        for l in lines[-c["runner_log_lines"]:]:
            m = re.match(r"^(\S+) LIMITED \S+ lane=(\w+) until (\S+ )?(\d{1,2}:\d{2})", l)
            if m and m.group(1)[:10] == today:
                day = (m.group(3) or today).strip()
                try:
                    if dt.datetime.fromisoformat(f"{day} {m.group(4)}") <= dt.datetime.now():
                        limits.pop(m.group(2), None)  # that limit has already lifted
                        continue
                except ValueError:
                    pass
                limits[m.group(2)] = words["until"].format(time=m.group(4))
    except OSError:
        pass
    busy = {}
    for r in running:
        if r["task"]:  # idle slot rows don't count
            busy[r["lane"]] = busy.get(r["lane"], 0) + 1
    o = c["ollama"]
    try:
        import urllib.request
        with urllib.request.urlopen(o["url"], timeout=o["timeout_s"]) as resp:
            loaded = [m.get("name") for m in json.loads(resp.read()).get("models", [])]
        local = o["loaded"].format(models=", ".join(loaded)) if loaded else o["empty"]
    except Exception:
        local = o["down"]
    exam_rates = _exam_pass_rates()
    bad = tuple(c["bad_status_prefixes"])
    rows = []
    for u in c["usage"]:
        lane = u["lane"]
        p = per.get(lane, {"runs": 0, "pass": 0, "minutes": 0.0, "cost": 0.0, "last": ""})
        n_slots = int(slots.get(lane, 0)) if u.get("slots") else 0
        if u.get("source") == "ollama":
            status = local
        elif u.get("status"):
            status = u["status"]
        elif lane in limits:
            status = words["limited"].format(limit=limits[lane])
        elif busy.get(lane):
            status = words["working"].format(busy=busy[lane], slots=n_slots)
        else:
            status = words["available"]
        rows.append({"model": u["label"], "lane": lane, "status": status, "plan": u["plan"],
                     "busy": busy.get(lane, 0), "slots": n_slots, "runs_today": p["runs"],
                     "passed_today": p["pass"], "minutes_today": round(p["minutes"]),
                     "est_cost_today": round(p["cost"], 2), "last_run": p["last"],
                     "exam_pass_rate": exam_rates.get(lane),
                     "state": ("bad" if status.startswith(bad)
                               else "busy" if busy.get(lane) else "ok")})
    return rows


def _outputs() -> list[dict]:
    """Finished tasks of the last outputs_days days, newest first, with one line of what changed."""
    c = _c()
    st = _state()
    rows = []
    cutoff = (dt.date.today() - dt.timedelta(days=c["outputs_days"] - 1)).isoformat()
    skip = (None, *c["skip_results"])
    for slug, ts in st.get("tasks", {}).items():
        for h in ts.get("history", []):
            end = str(h.get("end", ""))
            if end[:10] < cutoff or h.get("result") in skip:
                continue
            report = None
            for cand in [*(REVIEW / "archive").glob(f"*/{slug}.report.md"), REVIEW / "parked" / f"{slug}.report.md",
                         REVIEW / "reports" / f"{slug}.attempt-parked.md"]:
                if cand.exists():
                    report = cand
                    break
            done, title = "", slug
            if report:  # the Orchestra report format: a "DONE:" block of "- " lines
                text = report.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"^DONE:\s*\n((?:\s*-.*\n?)+)", text, re.M)
                if m:
                    done = re.sub(r"^\s*-\s*", "", m.group(1).strip().splitlines()[0])[:c["summary_chars"]]
            for cand in [*(REVIEW / "archive").glob(f"*/{slug}.md"), REVIEW / "queue" / f"{slug}.md",
                         REVIEW / "parked" / f"{slug}.md"]:
                if cand.exists():
                    title = _title(cand, slug)
                    break
            push = ts.get("push", "") if h.get("result") == "PASS" else ""
            rows.append({"id": f"{slug}@{end}", "when": end[5:16].replace("T", " "), "end": end,
                         "task": title[:c["title_chars"]], "result": h.get("result"), "lane": h.get("lane", "?"),
                         "minutes": round(h.get("seconds", 0) / 60), "pushed": "yes" if push.startswith("pushed") else "no",
                         "summary": done})
    rows.sort(key=lambda r: r["end"], reverse=True)
    return rows[:c["outputs_max"]]


def _activity() -> list[dict]:
    """Minutes of agent work per day and lane, last activity_days days (for the bar chart)."""
    c = _c()
    lanes = c["activity_lanes"]
    st = _state()
    days = [(dt.date.today() - dt.timedelta(days=i)).isoformat() for i in range(c["activity_days"] - 1, -1, -1)]
    acc = {(d, l): 0.0 for d in days for l in lanes}
    for ts in st.get("tasks", {}).values():
        for h in ts.get("history", []):
            key = (str(h.get("end", ""))[:10], h.get("lane"))
            if key in acc:
                acc[key] += h.get("seconds", 0) / 60
    return [{"day": d, "lane": l, "minutes": round(acc[(d, l)])} for d in days for l in lanes]


def _study() -> list[dict]:
    """One row per module from the study tutor's last `python -m tutor progress`
    (tutor/progress.py): lectures read vs on KEATS, mastery, weak topics, the next
    deadline and what to study next. Nothing run yet, or unreadable: no rows."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    try:
        from tutor import progress
        return progress.hub_rows()
    except Exception:  # noqa: BLE001 -- the hub never fails because of the tutor
        return []


TABLES = ("agents", "tasks", "waiting", "log", "learner", "spend", "running", "usage",
          "outputs", "activity", "study")


def tables(names=TABLES) -> dict:
    """The hub's tables by name, built from local state; reads only, writes nothing
    (`write` saves them for the claude.ai hub, atrium/live.py serves them)."""
    memo: dict = {}

    def tasks():
        if "tasks" not in memo:
            memo["tasks"] = _task_rows()
        return memo["tasks"]

    def running():
        if "running" not in memo:
            memo["running"] = _running()
        return memo["running"]

    build = {"agents": _agents, "tasks": tasks, "waiting": lambda: _waiting(tasks()), "log": _log,
             "learner": _learner, "spend": _spend, "running": running, "usage": lambda: _usage(running()),
             "outputs": _outputs, "activity": _activity, "study": _study}
    return {n: build[n]() for n in names}


def write(out: Path | None = None) -> Path:
    out = out or REVIEW / _c()["paths"]["hub"]
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in tables().items():
        (out / f"{name}.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    return out
