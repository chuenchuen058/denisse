"""The v3 mission board of the live Atrium page: its figures and its HTML.

live.collect() hands over what it already read (snapshot tables, the runner's state.json, the
agents, cursors, timeline, power); this adds the few reads the board needs on top (runner.log
lines, hot.md sections, Ollama /api/ps, the GPU, the exam and digest state files), all read-only,
and renders every section as HTML here, so the page app.js shows is the page a test can check.
Nothing here is sample data: a section with nothing to read shows the empty word. Every word,
colour, limit, path and log pattern: atrium/config.json "live" -> "board". Layout after
atrium/design/mission-board-reference.html.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import math
import re
import subprocess
import time
import urllib.request

from . import conf, context, snapshot

_cache: dict[str, tuple[float, object]] = {}

# The pointer's shape (geometry only; its colour is the agent's, its label the agent's).
ARROW = '<svg viewBox="0 0 10 14" aria-hidden="true"><path d="M0.5 0.5V12.5L3.8 9.4L6 13.6L8 12.7L5.8 8.6H10Z"/></svg>'


def _b() -> dict:
    return conf.section("live")["board"]


def e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _cached(key: str, ttl: float, read):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = read()
    _cache[key] = (time.monotonic(), value)
    return value


def _cut(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _when(t: dt.datetime, now: dt.datetime, today: str, other: str) -> str:
    return t.strftime(today if t.date() == now.date() else other)


def _dt(s) -> dt.datetime | None:
    return snapshot._local_dt(s) if s else None


def _none(words: dict) -> str:
    return f'<div class="none">{e(words["empty"])}</div>'


# -- reads ------------------------------------------------------------------------
def log_lines() -> list[tuple[dt.datetime, str]]:
    """(time, rest) of the runner.log's newest lines, oldest first; other lines are skipped."""
    c = _b()["runner_log"]
    try:
        raw = (snapshot.REVIEW / c["file"]).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for l in raw[-c["lines"]:]:
        stamp, _, rest = l.partition(" ")
        when = _dt(stamp)
        if when and rest:
            out.append((when, rest))
    return out


def _hot_bullets(note: str, section: str, skip_block: list[str] | None = None) -> list[str]:
    text = context._section(context._read(note), section)
    if skip_block:
        text = re.sub(re.escape(skip_block[0]) + r".*?" + re.escape(skip_block[1]), "", text, flags=re.S)
    return [re.sub(r"^\s*[-*]\s*", "", l).strip() for l in context._bullets(text).splitlines()]


def _ollama() -> dict | None:
    """Ollama's loaded models ({name, vram bytes}), or None when it does not answer."""
    o = snapshot._c()["ollama"]
    try:
        with urllib.request.urlopen(o["url"], timeout=o["timeout_s"]) as resp:
            models = json.loads(resp.read()).get("models", [])
        return {"models": [{"name": m.get("name") or "", "vram": m.get("size_vram") or 0} for m in models]}
    except Exception:  # noqa: BLE001 -- not running, or not Ollama: shown as down, never an error
        return None


def _gpu_line() -> str:
    c = _b()["local"]
    try:
        r = subprocess.run(c["gpu_command"], capture_output=True, text=True, timeout=c["gpu_timeout_s"])
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout.splitlines() or [""])[0] if r.returncode == 0 else ""


def _gpu() -> dict | None:
    """{pct, used, total (GB)} from the GPU command's first line, or None when it gives none."""
    c = _b()["local"]
    try:
        pct, used, total = (float(x) for x in _gpu_line().split(",")[:3])
    except ValueError:
        return None
    return {"pct": round(pct), "used": used / c["mib_per_gb"], "total": total / c["mib_per_gb"]}


def _json_file(path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


# -- figures ----------------------------------------------------------------------
def _runs(state: dict):
    for slug, ts in (state.get("tasks") or {}).items():
        for h in (ts or {}).get("history", []) or []:
            end = _dt(h.get("end"))
            if end:
                yield slug, h, end


def _running(x: dict) -> list[dict]:
    """The running tasks, each with when it started and the timeout the runner gave it."""
    c = _b()["runner_log"]
    start_re = re.compile(c["start_line"])
    timeouts = {}
    for _, rest in x["log"]:
        m = start_re.match(rest)
        if m:
            timeouts[m.group("task")] = int(m.group("timeout"))
    midnight = dt.datetime.combine(x["now"].date(), dt.time())
    files = {k["task_id"]: k["file"] for k in x["where"]["cursors"]}
    rows = []
    for r in x["t"]["running"]:
        if not r["task"]:
            continue
        since = _dt((x["state"].get("tasks") or {}).get(r["id"], {}).get("last_start")) \
            or x["now"] - dt.timedelta(minutes=r["minutes"])
        timeout = timeouts.get(r["id"])
        rows.append({"id": r["id"], "lane": r["lane"], "task": r["task"], "file": files.get(r["id"], ""),
                     "start_h": round((since - midnight).total_seconds() / 3600, 4), "timeout_s": timeout,
                     "stuck": bool(timeout and (x["now"] - since).total_seconds() > timeout)})
    return rows


def _fmt_n(n: float, c: dict) -> str:
    for size, unit in c["units"]:
        if n >= size:
            return c["unit_format"].format(n=n / size, unit=unit)
    return str(int(n))


def _ring_figures(x: dict) -> dict:
    usage = x["t"]["usage"]
    spend = (x["t"]["spend"] or [{}])[0]
    return {"pass": (sum(u.get("passed_today", 0) for u in usage), sum(u.get("runs_today", 0) for u in usage)),
            "slots": (sum(u.get("busy", 0) for u in usage), sum(u.get("slots", 0) for u in usage)),
            "ready": (sum(a["state"] != "bad" for a in x["agents"]), len(x["agents"])),
            "spend": (spend.get("cost_usd", 0) or 0, spend.get("cap_usd", 0) or 0)}


def _colour(name: str, x: dict) -> str:
    """A ring's or bar's colour: "lane:<lane>" = that agent's, "#..." as is, else a theme variable."""
    if name.startswith("lane:"):
        return next((a["colour"] for a in x["agents"] if a["lane"] == name[5:]), "var(--sub)")
    return name if name.startswith("#") else f"var(--{name})"


# -- sections ---------------------------------------------------------------------
def _header(x: dict, runs: list[dict]) -> tuple[dict, str]:
    w, lw, b = x["words"], x["live_words"], _b()
    stuck = sum(r["stuck"] for r in runs)
    limited = [a for a in x["agents"] if a["until"]]
    if stuck:
        level, text = "bad", w["verdict_stuck"].format(n=stuck)
    elif limited:
        a = limited[0]
        level, text = "warn", w["verdict_limited"].format(
            label=a["label"], time=_dt(a["until"]).strftime(w["verdict_time"]))
    else:
        level, text = "ok", w["verdict_ok"]
    p = x["power"]
    pill = w["pill"].format(working=len(runs), stuck=stuck)
    if p.get("battery_percent") is not None:
        pill += w["pill_power"].format(icon=lw["ac"] if p.get("ac") else lw["battery"], pct=p["battery_percent"])
    else:
        pill += w["pill_power_unknown"]
    head = (f'<div><div class="date"><span id="clock"></span><span class="sep">{e(b["clock"]["join"])}</span>'
            f'<span id="verdict" class="{level}">{e(text)}</span></div>'
            f'<h1 data-section="title">{e(b["title"])}</h1></div>'
            f'<div class="right"><div class="pill" data-section="pill"><span class="dot {level}" id="dot"></span>'
            f'<span>{e(pill)}</span><span id="state"></span></div>'
            f'<button class="theme" id="theme" type="button">{e(lw["theme"])}</button></div>')
    return {"level": level, "text": text, "pill": pill}, head


def _rings(x: dict) -> str:
    b = _b()
    g, figures, out = b["ring"], _ring_figures(x), []
    circ = 2 * math.pi * g["radius"]
    for r in b["rings"]:
        v, m = figures.get(r["key"], (0, 0))
        pct = min(100, round(100 * v / m)) if m else 0
        value = r["value"].format(v=v, m=m, pct=pct) if m else x["words"]["dash"]
        label = r["label"].format(v=v, m=m, pct=pct) if m else r["empty"]
        circle = (f'<circle cx="{g["box"] / 2}" cy="{g["box"] / 2}" r="{g["radius"]}" fill="none" '
                  f'stroke-width="{g["stroke"]}"')
        out.append(f'<div class="w rg" data-ring="{e(r["key"])}"><svg viewBox="0 0 {g["box"]} {g["box"]}">'
                   f'{circle} stroke="var(--line)"/>'
                   + (f'{circle} stroke="{e(_colour(r["colour"], x))}" stroke-linecap="round" '
                      f'stroke-dasharray="{circ * pct / 100:.1f} {circ:.1f}"/>' if pct else "")
                   + f'</svg><div><div class="v">{e(value)}</div><div class="l">{e(label)}</div></div></div>')
    return "".join(out)


def _lane_card(a: dict, x: dict, runs: list[dict]) -> str:
    w, c = x["words"], _b()["lane_card"]
    mine = [r for r in runs if r["lane"] == a["lane"]]
    u = x["usage"].get(a["lane"], {})
    body, meta = "", []
    if mine:
        state = w["state_working"]
        for r in mine[:c["max_tasks"]]:
            body += (f'<div class="task">{e(r["task"])}</div>'
                     + (f'<div class="file">{e(r["file"])}</div>' if r["file"] else "")
                     + f'<div class="prog"><i></i></div><div class="meta" data-timer data-start-h="{r["start_h"]}" '
                       f'data-timeout-s="{r["timeout_s"] or ""}"></div>')
    elif a["until"]:
        state = w["state_limited"]
        until = _dt(a["until"])
        meta.append(w["resets"].format(time=_when(until, x["now"], w["resets_today"], w["resets_other"])))
    elif a["state"] == "bad":
        state = w["state_down"]
        body = f'<div class="task">{e(a["status"])}</div>'
    elif a["queued"] and a["why"]:
        state = w["state_held"]
        body = f'<div class="task">{e(a["why"])}</div>'
    else:
        state = w["state_free"]
        body = f'<div class="task sub">{e(w["no_task"])}</div>'
    if a["queued"]:
        meta.append(a["queued"])
    badge = (f' <span class="badge">{e(w["slots"].format(busy=u.get("busy", 0), slots=u["slots"]))}</span>'
             if u.get("slots", 0) > 1 else "")
    return (f'<h3>{e(a["label"])}</h3><div class="name">{e(state)}{badge}</div>{body}'
            + (f'<div class="meta end">{e(w["join"].join(meta))}</div>' if meta else ""))


def _seat(k: dict, colours: dict, cls: str) -> str:
    return (f'<span class="cur {cls}" data-seat="{e(k["id"])}" style="--c:{e(colours.get(k["lane"], ""))}">'
            f'{ARROW}<b>{e(k["label"])}</b></span>')


def _cursor_rows(x: dict) -> str:
    """One row per touched file (and per working agent with no file yet); a cursor's seat is
    where app.js parks the gliding pointer."""
    W, lw = x["where"], x["live_words"]
    colours = {a["lane"]: a["colour"] for a in x["agents"]}
    by_id = {k["id"]: k for k in W["cursors"]}
    if not W["cursors"]:
        return f'<div class="none">{e(lw["nobody_working"])}</div>'
    warn = {o["path"]: o["text"] for o in W["overlaps"]}
    rows = []
    for k in W["cursors"]:
        if not k["file"]:
            rows.append(f'<div class="row task-row" title="{e(k["task"])}"><span class="t">{e(k["task"])}</span>'
                        f'{_seat(k, colours, "seat")}</div>')
    for f in W["files"]:
        marks = "".join(_seat(by_id[i], colours, "seat" if by_id[i]["file"] == f["path"] else "mini")
                        for i in f["who"] if i in by_id)
        rows.append(f'<div class="row{" shared" if f["shared"] else ""}" title="{e(f["path"])}">'
                    f'<span class="t">{e(f["path"])}</span>{marks}</div>'
                    + (f'<div class="warn">{e(W["icon"])} {e(warn.get(f["path"], ""))}</div>' if f["shared"] else ""))
    if not W["files"]:
        rows.append(f'<div class="none">{e(lw["no_files"])}</div>')
    if W["more_files"]:
        rows.append(f'<div class="more">{e(W["more_files"])}</div>')
    return "".join(rows)


def _merged(x: dict) -> str:
    w = x["words"]
    merged_re = re.compile(_b()["runner_log"]["merged_line"])
    merged = sum(1 for when, rest in x["log"] if when.date() == x["now"].date() and merged_re.match(rest))
    finished = sum(1 for _, _, end in _runs(x["state"]) if end.date() == x["now"].date())
    return f'<div class="big">{merged}</div><div class="why">{e(w["merged_of"].format(n=finished))}</div>'


def _needs_look(x: dict) -> tuple[str, bool]:
    w = x["words"]
    parked = sum(t["status"] == "parked" for t in x["t"]["tasks"])
    drafts = sum(t["status"] == "draft" for t in x["t"]["tasks"])
    sub = w["needs_sub"].format(parked=parked, drafts=drafts) if parked or drafts else w["needs_none"]
    return f'<div class="big">{parked}</div><div class="why">{e(sub)}</div>', parked > 0


def _queued(x: dict) -> list[dict]:
    order = _b()["up_next"]["priority_order"]
    held = {r["id"] for r in x["t"]["running"] if r["task"]}
    rows = [t for t in x["t"]["tasks"] if t["status"] == "queued" and t["id"] not in held]
    rows.sort(key=lambda t: (order.index(t["priority"]) if t["priority"] in order else len(order), t["id"]))
    return rows


def _up_next(x: dict) -> str:
    by_lane = {a["lane"]: a for a in x["agents"]}
    rows = _queued(x)[:_b()["up_next"]["max"]]
    if not rows:
        return _none(x["words"])
    items = []
    for t in rows:
        a = by_lane.get(t["lane"], {})
        why = x["words"]["join"].join(s for s in (a.get("label", t["lane"]), a.get("why", "")) if s)
        items.append(f'<li><span class="t">{e(t["task"])}</span><span class="why">{e(why)}</span></li>')
    return f'<ul>{"".join(items)}</ul>'


def _node(cls: str, text: str, small: str = "") -> str:
    return (f'<span class="nd {e(cls)}"><i></i>{e(text)}'
            + (f' <small>{e(small)}</small>' if small else "") + '</span>')


def _tree(x: dict) -> str:
    c = _b()["tree"]
    done_re = re.compile(c["done_pattern"])
    skip = {s.upper() for s in c["skip"]}
    labels = {a["lane"]: a["label"] for a in x["agents"]}
    branches = []
    for g in c["groups"]:
        kids = []
        if g["source"] == "hot":
            opened, done = [], []
            for t in _hot_bullets(c["note"], g["section"], c["skip_block"]):
                if not t or t.upper() in skip:
                    continue
                m = done_re.match(t)
                (done if m else opened).append(_cut(m.group("text") if m else t, c["item_chars"]))
            kids = ([f'<li>{_node(c["open_class"], t)}</li>' for t in opened[:c["max_open"]]]
                    + [f'<li>{_node(c["done_class"], t)}</li>' for t in done[:c["max_done"]]])
        elif g["source"] == "queue":
            queued = {t["id"] for t in _queued(x)}
            for s in g["statuses"]:
                rows = [t for t in x["t"]["tasks"] if t["status"] == s["status"]
                        and (s["status"] != "queued" or t["id"] in queued)]
                if not rows:
                    continue
                leaves = "".join(
                    "<li>" + _node(g["urgent_class"] if t["priority"] in g["urgent_priorities"] else s["class"],
                                   _cut(t["task"], c["item_chars"]), labels.get(t["lane"], t["lane"])) + "</li>"
                    for t in rows[:g["max_per_status"]])
                more = len(rows) - g["max_per_status"]
                if more > 0:
                    leaves += f'<li>{_node(s["class"], x["live_words"]["more_files"].format(n=more))}</li>'
                kids.append(f'<li>{_node(s["class"], s["label"], str(len(rows)))}<ul>{leaves}</ul></li>')
        if kids:
            branches.append(f'<ul><li><span class="nd">{e(g["title"])}</span><ul>{"".join(kids)}</ul></li></ul>')
    if not branches:
        return _none(x["words"])
    legend = "".join(_node(l["class"], l["text"]) for l in c["legend"])
    return f'<div class="tree">{"".join(branches)}</div><div class="leg">{legend}</div>'


def _tile(name: str, dot: str, s: str = "", m: str = "", f: str = "") -> str:
    return (f'<div class="lt"><div class="n"><span class="sd {dot}"></span>{e(name)}</div>'
            f'<div class="s">{e(s)}</div>' + (f'<div class="m">{e(m)}</div>' if m else "")
            + (f'<div class="f">{e(f)}</div>' if f else "") + '</div>')


def _local(x: dict) -> str:
    c = _b()["local"]
    w, empty, now = c["words"], x["words"]["empty"], x["now"]
    stamp = lambda t: _when(t, now, c["time_today"], c["time_other"])
    tiles = []
    for t in c["tiles"]:
        kind, name = t["kind"], t["name"]
        if kind == "ollama":
            o = _cached("ollama", c["cache_s"], _ollama)
            if o is None:
                tiles.append(_tile(name, "bad", w["ollama_down"]))
                continue
            names = ", ".join(m["name"] for m in o["models"]) or w["no_model"]
            vram = sum(m["vram"] for m in o["models"]) / c["bytes_per_gb"]
            tiles.append(_tile(name, "run" if o["models"] else "", w["ollama_up"], names,
                               w["vram"].format(used=vram) if vram else ""))
        elif kind == "gpu":
            g = _cached("gpu", c["cache_s"], _gpu)
            tiles.append(_tile(name, "idle", empty) if g is None else
                         _tile(name, "", w["gpu_busy"].format(pct=g["pct"]),
                               w["gpu_mem"].format(used=g["used"], total=g["total"])))
        elif kind == "don":
            consult_re = re.compile(_b()["runner_log"]["consult_line"])
            hits = [(when, m) for when, rest in x["log"] if when.date() == now.date()
                    for m in [consult_re.match(rest)] if m]
            if not hits:
                tiles.append(_tile(name, "idle", w["no_consults"]))
                continue
            when, m = hits[-1]
            tiles.append(_tile(name, "", w["consults"].format(n=len(hits)), "",
                               w["last_consult"].format(time=stamp(when), confidence=m.group("confidence"))))
        elif kind == "evals":
            run = _json_file(snapshot.REVIEW / c["evals_file"])
            rows = {k[len(c["evals_prefix"]):]: v for k, v in (run.get("results") or {}).items()
                    if k.startswith(c["evals_prefix"])}
            got = [re.match(r"^(\d+)/(\d+)$", str(v)) for v in rows.values()]
            if not rows or not all(got):
                tiles.append(_tile(name, "idle", empty))
                continue
            v, m = sum(int(g.group(1)) for g in got), sum(int(g.group(2)) for g in got)
            done = _dt(run.get("finished"))
            tiles.append(_tile(name, "", w["evals"].format(v=v, m=m),
                               w["eval_join"].join(w["eval_row"].format(name=k, result=r) for k, r in rows.items()),
                               w["evals_last"].format(time=stamp(done)) if done else ""))
        elif kind == "digest":
            last = _dt(_json_file(context.VAULT / conf.section("digest")["state_file"]).get("last"))
            tiles.append(_tile(name, "", w["digest_last"].format(time=stamp(last))) if last
                         else _tile(name, "idle", empty))
    return f'<div class="local">{"".join(tiles)}</div>' if tiles else _none(x["words"])


def _token_sum(tokens, keys) -> int:
    return sum(int(tokens.get(k) or 0) for k in keys) if isinstance(tokens, dict) else 0


def _tokens(x: dict) -> str:
    c, w, now = _b()["tokens"], x["words"], x["now"]
    runs = [(h, end) for _, h, end in _runs(x["state"]) if isinstance(h.get("tokens"), dict)]
    if not runs:
        return _none(w)
    week_from = now.date() - dt.timedelta(days=c["week_days"] - 1)
    inside = {"today": lambda d: d.date() == now.date(), "week": lambda d: d.date() >= week_from,
              "month": lambda d: (d.year, d.month) == (now.year, now.month), "all": lambda d: d.date() >= all_from}
    first = min(end for _, end in runs)
    all_from = dt.date.fromisoformat(c["all_from"]) if c.get("all_from") else first.date()
    since = dt.datetime.combine(all_from, dt.time()).strftime(c["since_format"])
    first_s = first.strftime(c["since_format"])
    cells = []
    for p in c["periods"]:
        rows = [h for h, end in runs if inside[p["key"]](end)]
        total = sum(_token_sum(h["tokens"], c["all_keys"]) for h in rows)
        fresh = sum(_token_sum(h["tokens"], c["fresh_keys"]) for h in rows)
        label = p["label"].format(fresh=_fmt_n(fresh, c), days=c["week_days"], since=since, first=first_s)
        cells.append(f'<div><div class="big mid">{e(_fmt_n(total, c) if rows else w["dash"])}</div>'
                     f'<div class="why">{e(label)}</div></div>')
    per = {}
    for h, end in runs:
        if not inside["all"](end):
            continue
        per[h.get("lane")] = per.get(h.get("lane"), 0) + _token_sum(h["tokens"], c["all_keys"])
    lanes = c["lane_join"].join(
        (c["lane"].format(label=a["label"], tokens=_fmt_n(per[a["lane"]], c)) if a["lane"] in per
         else c["lane_unknown"].format(label=a["label"])) for a in x["agents"])
    return (f'<div class="cells">{"".join(cells)}</div>'
            f'<div class="why note">{e(lanes)}{e(c["lane_join"])}{e(c["note"])}</div>')


def _credits(x: dict) -> str:
    c, tc, w, now = _b()["credits"], _b()["tokens"], x["words"], x["now"]
    labels = {a["lane"]: a["label"] for a in x["agents"]}
    runs = sorted(((slug, h, end) for slug, h, end in _runs(x["state"]) if end.date() == now.date()),
                  key=lambda r: r[2], reverse=True)[:c["max"]]
    if not runs:
        return _none(w)
    items = []
    for slug, h, _ in runs:
        spent = str(h.get("spent") or "")
        f = {"label": labels.get(h.get("lane"), h.get("lane") or ""),
             "spent": spent if any(ch.isdigit() for ch in spent) else c["spent_unknown"],
             "tokens": _fmt_n(_token_sum(h.get("tokens"), tc["all_keys"]), tc)}
        row = (c["row"] if isinstance(h.get("tokens"), dict) else c["row_no_tokens"]).format(**f)
        items.append(f'<li><span class="t">{e(x["titles"].get(slug, slug))}</span><span class="why">{e(row)}</span></li>')
    return f'<ul>{"".join(items)}</ul>'


def _limits(x: dict) -> str:
    c, w, now = _b()["limits"], x["words"], x["now"]
    lanes = x["state"].get("lanes") if isinstance(x["state"].get("lanes"), dict) else {}
    if not x["agents"]:
        return _none(w)
    rows = []
    for a in x["agents"]:
        rec = lanes.get(a["lane"]) if isinstance(lanes.get(a["lane"]), dict) else {}
        until, since = _dt(rec.get("limited_until")), _dt(rec.get("since"))
        if until and until > now:
            span = (until - since).total_seconds() if since and since < until else 0
            pct = round(min(100, max(0, 100 * (now - since).total_seconds() / span))) if span else 0
            text, cls = w["limit_until"].format(time=_when(until, now, c["time_today"], c["time_other"])), "cd bad"
        else:
            pct, text, cls = 0, w["limit_none"], "cd sub"
        rows.append(f'<div class="reset" style="--c:{e(a["colour"])}"><b>{e(a["label"])}</b>'
                    f'<div class="bar"><i style="width:{pct}%"></i></div><span class="{cls}">{e(text)}</span></div>')
    return "".join(rows)


def _activity(x: dict) -> str:
    c, now = _b()["activity"], x["now"]
    labels = {a["lane"]: a["label"] for a in x["agents"]}
    events = [(re.compile(ev["pattern"]), ev["text"]) for ev in c["events"]]
    items = []
    for when, rest in reversed(x["log"]):
        for pat, text in events:
            m = pat.match(rest)
            if not m:
                continue
            f = m.groupdict()
            f["title"] = x["titles"].get(f.get("task", ""), f.get("task", ""))
            f["label"] = labels.get(f.get("lane", ""), f.get("lane", ""))
            items.append(f'<li><span class="t">{e(_when(when, now, c["time_today"], c["time_other"]))}</span>'
                         f'<span>{e(_cut(text.format(**f), c["text_chars"]))}</span></li>')
            break
        if len(items) >= c["max"]:
            break
    return f'<ul id="feed">{"".join(items)}</ul>' if items else _none(x["words"])


def _today(x: dict) -> str:
    c, w = _b()["today"], x["words"]
    rows = sorted(x["timeline"], key=lambda r: r["start"], reverse=True)[:c["max"]]
    if not rows:
        return _none(w)
    items = []
    for r in rows:
        at = c["time"].format(h=int(r["start"]), m=int(round((r["start"] % 1) * 60)) % 60)
        result = r["result"] or (w["result_running"] if r["end"] is None else "")
        items.append(f'<li><b>{e(at)}</b><span class="t">{e(r["task"])}</span>'
                     f'<span class="why {e(r["cls"])}">{e(result)}</span></li>')
    return f'<ul>{"".join(items)}</ul>'


def _projects(x: dict) -> str:
    c = _b()["projects"]
    lead = re.compile(c["lead_pattern"])
    skip = tuple(s.upper() for s in c["skip"])
    items = []
    for t in _hot_bullets(c["note"], c["section"]):
        if not t or t.upper().startswith(skip):
            continue
        m = lead.match(t)
        who, text = (m.group("who") or "", m.group("text")) if m else ("", t)
        items.append(f'<li><b>{e(who)}</b><span>{e(_cut(text, c["item_chars"]))}</span></li>')
    return f'<ul>{"".join(items[:c["max"]])}</ul>' if items else _none(x["words"])


def _progress(x: dict) -> str:
    if not x["progress"]:
        return _none(x["words"])
    return "".join(f'<div class="reset wide" style="--c:{e(_colour(p["colour"], x))}"><b>{e(p["label"])}</b>'
                   f'<div class="bar"><i style="width:{p["pct"]}%"></i></div><span class="cd">{e(p["text"])}</span></div>'
                   for p in x["progress"])


def _markets(x: dict) -> str:
    c = _b()["markets"]
    items = []
    for r in x["markets"] or []:
        if not isinstance(r, dict) or not r.get("ticker"):
            continue
        try:
            pct = float(r.get("day_change_pct"))
            change = c["row_change"].format(sign=c["up"] if pct >= 0 else c["down"], pct=pct)
            cls = "ok" if pct >= 0 else "bad"
        except (TypeError, ValueError):
            change, cls = "", ""
        items.append(f'<li><b>{e(r["ticker"])}</b><span class="t">{e(r.get("price", ""))}</span>'
                     f'<span class="why {cls}">{e(change)}</span></li>')
    return f'<ul>{"".join(items)}</ul>' if items else _none(x["words"])


# -- the board --------------------------------------------------------------------
def build(x: dict) -> dict:
    """The board for live.collect()'s data. x: now, t (snapshot tables), state, agents, usage,
    where, timeline, progress, markets, power, live_words, titles."""
    b = _b()
    x = dict(x, words=b["words"], log=log_lines())
    runs = _running(x)
    verdict, header = _header(x, runs)
    plain = {"tree": _tree, "local": _local, "merged": _merged, "up_next": _up_next, "tokens": _tokens,
             "credits": _credits, "limits": _limits, "activity": _activity, "today": _today,
             "projects": _projects, "progress": _progress, "markets": _markets}
    cards = []
    for card in b["cards"]:
        cid, cls = card["id"], card.get("class", "")
        heading = b["headings"].get(cid, "")
        h3 = f"<h3>{e(heading)}</h3>"
        if cid == "lanes":
            for a in x["agents"]:
                busy = any(r["lane"] == a["lane"] for r in runs)
                cards.append({"key": f'lane:{a["lane"]}', "section": "lane",
                              "cls": f'agent {cls}{"" if busy else " idle"}'.strip(),
                              "style": f'--c:{a["colour"]}', "html": _lane_card(a, x, runs)})
        elif cid == "cursors":
            rows = _cursor_rows(x)
            cards.append({"key": cid, "section": cid, "cls": f"ws {cls}".strip(), "style": "", "heading": heading,
                          "rows": rows,
                          "html": f'{h3}<div class="where"><div class="rows">{rows}</div>'
                                  f'<div class="layer" aria-hidden="true"></div></div>'})
        elif cid == "needs_look":
            body, on = _needs_look(x)
            cards.append({"key": cid, "section": cid, "cls": f'{cls}{" alert" if on else ""}'.strip(), "style": "",
                          "html": h3 + body})
        elif cid in plain:
            cards.append({"key": cid, "section": cid, "cls": f"card-{cid} {cls}".strip(), "style": "",
                          "html": h3 + plain[cid](x)})
    theme = b["theme"]
    return {"title": b["title"], "verdict": verdict, "header": header, "rings": _rings(x), "cards": cards,
            "theme": theme, "theme_storage_key": b["theme_storage_key"], "clock": b["clock"],
            "favicon": dict(b["favicon"], arcs=[a["colour"] for a in x["agents"]],
                            dot=theme[b["favicon"]["scheme"]][verdict["level"]], dot_r=b["favicon"]["dot"]),
            "cursor_cfg": x["where"]["cursor_cfg"], "bar_full_pct": b["lane_card"]["bar_full_pct"],
            "words": {k: b["words"][k] for k in ("timer", "timer_limit")}}


def card_html(card: dict) -> str:
    style = f' style="{e(card["style"])}"' if card["style"] else ""
    return (f'<div class="w {e(card["cls"])}" data-key="{e(card["key"])}" data-section="{e(card["section"])}"'
            f'{style}>{card["html"]}</div>')


def render(data: dict) -> str:
    """The whole page as one HTML document, from collect()'s data: the same header, rings and
    cards app.js puts on screen (it only adds the clock, the timers and the gliding pointers)."""
    b = data["board"]
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{e(b["title"])}</title></head><body>'
            f'<header class="top" id="top" data-section="header">{b["header"]}</header>'
            f'<div class="rings" id="rings" data-section="rings">{b["rings"]}</div>'
            f'<div class="grid" id="grid">{"".join(card_html(c) for c in b["cards"])}</div></body></html>')
