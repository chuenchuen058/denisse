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


# -- reads, cached on what the file is ----------------------------------------------
# The live page rebuilds every few seconds while anything changes; a rebuild used to read every
# task file (~600) twice and parse the 1.4 MB state.json four times (15 Nov 2026). A file's text
# is kept against its (mtime, size); a JSON file's parse against its bytes, so a rewrite inside
# one clock tick is still seen. Callers treat what these return as read-only.
_texts: dict[str, tuple[tuple, str]] = {}
_jsons: dict[str, tuple[bytes, object]] = {}


def _key(p: Path) -> tuple:
    s = p.stat()
    return s.st_mtime_ns, s.st_size


def read_text(p: Path) -> str:
    """p's text (utf-8, undecodable bytes replaced), re-read only when its mtime or size moved.
    OSError as Path.read_text."""
    key = _key(p)
    hit = _texts.get(str(p))
    if hit and hit[0] == key:
        return hit[1]
    text = p.read_text(encoding="utf-8", errors="replace")
    _texts[str(p)] = (key, text)
    return text


def read_json(p: Path) -> tuple[object, str]:
    """(parsed, "") or (None, why): why is "missing", "unreadable", "bad_json" (the words for
    each: live board sources.why). Parsed again only when the bytes changed."""
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    hit = _jsons.get(str(p))
    if hit and hit[0] == raw:
        return hit[1], ""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return None, "bad_json"
    _jsons[str(p)] = (raw, data)
    return data, ""


def read_state() -> tuple[dict, str]:
    """The runner's state.json and what is wrong with it ("" when nothing: the why words of
    read_json, or "not_object"). Unreadable reads as an empty state."""
    data, why = read_json(REVIEW / "state.json")
    if not why and not isinstance(data, dict):
        why = "not_object"
    return (data if not why else {"tasks": {}, "days": {}}), why


def tail_lines(p: Path, n: int) -> list[str]:
    """The last n lines of p (a growing log), [] when it can't be read."""
    try:
        return read_text(p).splitlines()[-n:] if n else []
    except OSError:
        return []


_tails: dict[str, tuple[tuple, str]] = {}


def tail_chars(p: Path, n: int) -> str:
    """The last n characters of p (up to 4 bytes each read from the end, not the whole log),
    kept against its (mtime, size). OSError as Path.read_text."""
    key = _key(p)
    hit = _tails.get(str(p))
    if hit and hit[0] == key:
        return hit[1]
    with open(p, "rb") as f:
        f.seek(max(0, key[1] - 4 * n))
        text = f.read().decode("utf-8", errors="replace")[-n:]
    _tails[str(p)] = (key, text)
    return text


def _title(p: Path, default: str) -> str:
    """A task file's "# " heading (the task file format), else default."""
    return next((l[2:].strip() for l in read_text(p).splitlines() if l.startswith("# ")), default)


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
    state = _state()
    rows = []
    # The lane the runner picked at dispatch (orchestra lane_pick.py writes these header names,
    # orchestra config lane_pick_headers) is the lane shown; the header's own Lane: is lane_asked.
    heads = _config().get("lane_pick_headers") or {}

    def header(text: str, name) -> str:
        m = re.search(rf"^{re.escape(name)}:\s*(.*?)\s*$", text, re.M | re.I) if name else None
        return m.group(1) if m else ""

    def add(p: Path, where: str):
        text = read_text(p)
        title = _title(p, p.stem)
        f = dict(re.findall(r"^(Queue|Kind|Lane|Priority):\s*(\S+)", text, re.M))
        ts = (state.get("tasks") or {}).get(p.stem) or {}
        status = where if where != "queue" else ("draft" if f.get("Queue", "").lower() != "auto" else "queued")
        asked = f.get("Lane", dflt["lane"])
        picked = header(text, heads.get("picked"))
        rows.append({"task": title[:n], "status": status, "priority": f.get("Priority", dflt["priority"]),
                     "lane": picked or asked, "lane_asked": asked, "pick_why": header(text, heads.get("why")),
                     "attempts": ts.get("attempts", 0),
                     "last_result": ts.get("last_result") or "", "id": p.stem})

    for p in sorted((REVIEW / "queue").glob("*.md")):
        if is_task_file(p):
            add(p, "queue")
    for p in sorted((REVIEW / "parked").glob("*.md")):
        if is_task_file(p):
            add(p, "parked")
    days = sorted((REVIEW / "archive").glob("*"))[-c["archive_days"]:] if (REVIEW / "archive").is_dir() else []
    for d in days:
        for p in sorted(d.glob("*.md")):
            if is_task_file(p):
                add(p, "done")
    return rows


def is_task_file(p: Path) -> bool:
    """A task file, not one of its side files (<slug>.report.md, <slug>.review.md, ...: config
    snapshot task_side_suffixes), which share its folder and its .md ending."""
    return not p.name.endswith(tuple(_c()["task_side_suffixes"]))


def done_line(text: str) -> bool:
    """A bullet that is done: struck through (it opens with one of waiting.struck_open) or
    matching one of waiting.done_markers (case-insensitive; one that does not compile is ignored)."""
    w = _c()["waiting"]
    t = re.sub(r"^\s*[-*]\s*", "", text).strip()
    if any(t.startswith(s) for s in w["struck_open"]):
        return True
    for pat in w["done_markers"]:
        try:
            if re.search(pat, t, re.I):
                return True
        except re.error:
            continue
    return False


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
        if t and t.upper() not in skip and not done_line(t):
            row = {"item": t[:w["item_chars"]], "from": w["from"], "kind": "note"}
            m = re.match(w["limit_line"], t, re.I)
            if m:  # aftercare's "Lane codex limited until 2026-11-11 12:08": gone once it lifts
                until = _local_dt(m.group(1))
                if until and until <= dt.datetime.now():
                    continue
                if until:
                    row["until"] = _iso_local(until)
            rows.append(row)
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
    rows = [{"item": r["item"], "from": r["from"], "until": r.get("until", "")} for r in waiting_items(tasks)]
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
    st, why = read_state()
    cfg, cfg_why = read_json(REPO / _c()["paths"]["orchestra_config"])
    if why or cfg_why or not isinstance(cfg, dict):
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


def _local_dt(text: str) -> dt.datetime | None:
    """A runner time ("2026-11-11T12:08:00", "2026-11-11 12:08", with or without an offset) as
    naive local time, the runner's own clock; None if it isn't one."""
    try:
        when = dt.datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None
    return when.astimezone().replace(tzinfo=None) if when.tzinfo else when


def _iso_local(when: dt.datetime) -> str:
    """Naive local time as ISO with this laptop's UTC offset, so the hub page (any viewer's
    clock and zone) can tell when it has passed."""
    return when.astimezone().isoformat(timespec="minutes")


def lane_limits(st: dict, now: dt.datetime | None = None) -> dict[str, dt.datetime]:
    """lane -> when it may run again, from the runner's own record (state.json lanes.<lane>.
    limited_until, written by orchestra runner._limited and read by its lane_limited). Only
    limits still in force; this record carries a reset past midnight, unlike runner.log."""
    now = now or dt.datetime.now()
    out = {}
    for lane, v in (st.get("lanes") or {}).items():
        until = _local_dt(v.get("limited_until") or "") if isinstance(v, dict) else None
        if until and until > now:
            out[lane] = until
    return out


def _state() -> dict:
    return read_state()[0]


def _config() -> dict:
    data, _ = read_json(REPO / _c()["paths"]["orchestra_config"])
    return data if isinstance(data, dict) else {}


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


def _allowance() -> dict:
    """lane -> Orchestra's allowance plan for it (orchestra/allowance.py): estimated % left in
    its tightest window, next reset, and whether it is held, conserved, spent down or spread.
    Reads orchestra's config and .review/<allowance_file>; anything unreadable: no rows."""
    try:
        orch = REPO / "orchestra"
        if str(orch) not in sys.path:
            sys.path.insert(0, str(orch))
        import allowance
        cfg = {}
        for name in ("config_defaults.json", "config.json"):
            try:
                cfg.update({k: v for k, v in json.loads((orch / name).read_text(encoding="utf-8")).items()
                            if not k.startswith("_")})
            except (OSError, ValueError):
                pass
        now = dt.datetime.now().replace(microsecond=0)
        limited = {}
        for lane, v in (_state().get("lanes") or {}).items():
            try:
                until = dt.datetime.fromisoformat((v or {}).get("limited_until") or "")
            except ValueError:
                continue
            if until > now:
                limited[lane] = until
        data = allowance.load(REVIEW / str(cfg.get("allowance_file", "allowance.json")))
        return {r["lane"]: r for r in allowance.rows(cfg, data, list(cfg.get("allowance") or {}), now, limited)}
    except Exception:  # noqa: BLE001 -- the hub never fails because of the allowance file
        return {}


def _usage(running: list[dict]) -> list[dict]:
    """One row per model: can it take work now, and what it did today, plus Orchestra's
    allowance plan for the lane (allowance_*: estimated % left, next reset, held / conserve /
    spend / spread; None where it has no model or reading)."""
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
            tail = tail_chars(log, c["lane_log_chars"])
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
        for l in tail_lines(REVIEW / "runner.log", c["runner_log_lines"]):
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
    # the runner's own record wins: it is what dispatch obeys (11 Nov 2026: a Codex limit hit
    # on 10 Nov until 12:08 on the 11th showed "available", as runner.log was read for today only)
    resets = lane_limits(st)
    for lane, until in resets.items():
        fmt = c["until_format"] if until.date() == dt.date.today() else c["until_day_format"]
        limits[lane] = words["until"].format(time=until.strftime(fmt))
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
    plan = _allowance()
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
                     "limited_until": _iso_local(resets[lane]) if lane in resets and not u.get("status") else "",
                     "allowance_left_pct": (plan.get(lane) or {}).get("left_pct"),
                     "allowance_window": (plan.get(lane) or {}).get("window"),
                     "allowance_resets": (plan.get(lane) or {}).get("resets"),
                     "allowance_plan": (plan.get(lane) or {}).get("mode"),
                     "allowance_why": (plan.get(lane) or {}).get("why"),
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


def tables(names=TABLES, failed: dict | None = None) -> dict:
    """The hub's tables by name, built from local state; reads only, writes nothing
    (`write` saves them for the claude.ai hub, atrium/live.py serves them). With a `failed`
    dict (the live page) a table whose build raises is empty and failed[name] says why,
    so one bad file costs its own card, not the page."""
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
    if failed is None:
        return {n: build[n]() for n in names}
    out = {}
    for n in names:
        try:
            out[n] = build[n]()
        except Exception as exc:  # noqa: BLE001 -- shown on the cards that use it (live board sources)
            out[n], failed[n] = [], type(exc).__name__
    return out


def write(out: Path | None = None) -> Path:
    out = out or REVIEW / _c()["paths"]["hub"]
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in tables().items():
        (out / f"{name}.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    return out
