"""What makes the live Atrium page interactive, on the server side (Chuen, 15 Nov 2026).

atrium/board.py calls in here for the three bits of HTML the interactions need, so its own
changes stay a few lines:

  lane_detail()  the block a lane card opens to show (files touched, the runner's last lines
                 about its task; for an idle lane, what it finished today and what is queued)
  filter_bar()   the segmented control and the quick filter over "What to work on"
  task_detail()  one task's drawer: its last run, the report's fields, the reviewer's verdict;
                 served by atrium/live.py on its own read-only path
  page_cfg()     what app.js needs to know (selectors, keys, durations, the drawer's words)

Read-only, like the rest of the page. Every word, selector, limit, file pattern and report
field name: atrium/config.json "live" -> "board" -> "interactive".
"""
from __future__ import annotations

import glob
import json
import re

from . import board, conf, snapshot


def _i() -> dict:
    return conf.section("live")["board"]["interactive"]


e = lambda s: board.e(s)  # noqa: E731 -- the board's own escaping


def page_cfg() -> dict:
    """The part of the config app.js acts on. No file pattern or server-side limit is sent."""
    i = _i()
    return {"vars": i["vars"], "expand": i["expand"]["cards"], "rows": i["drawer"]["rows"],
            "path": i["drawer"]["path"], "words": {k: i["drawer"]["words"][k] for k in i["drawer"]["page_words"]},
            "filter": {k: i["filter"][k] for k in ("storage_key", "default", "rows")},
            "count": i["count"], "tips": i["tips"], "keys": i["keys"]}


# -- a lane card, opened ------------------------------------------------------------
def _block(label: str, rows: list[str], empty: str) -> str:
    return f'<div class="mh">{e(label)}</div>' + ("".join(rows) or f'<div class="none">{e(empty)}</div>')


def _line(time: str, text: str, result: str = "", cls: str = "", task: str = "") -> str:
    return (f'<div class="lg"{f" data-task={chr(34)}{e(task)}{chr(34)}" if task else ""}>'
            + (f'<span class="t">{e(time)}</span>' if time else "") + f'<span class="x">{e(text)}</span>'
            + (f'<span class="why {e(cls)}">{e(result)}</span>' if result else "") + '</div>')


def lane_detail(a: dict, x: dict, mine: list[dict]) -> str:
    """The hidden part of a lane card. A working lane: per task, the files its worktree
    touched and the runner.log's newest lines about it. An idle one: today's finished runs.
    Either way, what is queued for the lane. All from data the board already read."""
    c = _i()["expand"]
    w, empty = c["words"], x["words"]["empty"]
    seat = {k["task_id"]: k["id"] for k in x["where"]["cursors"]}
    parts = []
    for r in mine:
        files = [f'<div class="file">{e(f["path"])}</div>' for f in x["where"]["files"]
                 if seat.get(r["id"]) in f["who"]][:c["max_files"]]
        lines = [_line(when.strftime(c["log_time"]),
                       board._cut(re.sub(r"\s+:", ":", rest.replace(r["id"], "")), c["log_chars"]))
                 for when, rest in x["log"] if r["id"] in rest][-c["max_log"]:]
        parts += [_block(w["files"], files, empty), _block(w["log"], lines, empty)]
    if not mine:
        t = board._b()["today"]["time"]
        done = sorted((r for r in x["timeline"] if r["lane"] == a["lane"] and r["end"] is not None),
                      key=lambda r: r["start"], reverse=True)[:c["max_runs"]]
        parts.append(_block(w["earlier"], [
            _line(t.format(h=int(r["start"]), m=int(round((r["start"] % 1) * 60)) % 60), r["task"],
                  r["result"], r["cls"], r.get("id", "")) for r in done], empty))
    queued = [t for t in board._queued(x) if t["lane"] == a["lane"]][:c["max_queued"]]
    if queued:
        parts.append(_block(w["queued"], [_line("", t["task"], task=t["id"]) for t in queued], empty))
    return f'<div class="more"><div>{"".join(parts)}</div></div>'


# -- the filter over "What to work on" ------------------------------------------------
def group_key(g: dict) -> str:
    """A tree group's name for the filter: its "key", else its title."""
    return g.get("key") or g["title"]


def filter_bar() -> str:
    """The segmented control (app.js slides the one highlight under the chosen segment and
    hides the other groups) and the quick filter's box."""
    f = _i()["filter"]
    segs = "".join(f'<button type="button" data-seg="{e(s["key"])}" data-groups="{e(json.dumps(s["groups"]))}">'
                   f'{e(s["label"])}</button>' for s in f["segments"])
    return (f'<div class="tools"><div class="seg" data-filter><i class="slide"></i>{segs}</div>'
            f'<input class="q" id="q" type="search" autocomplete="off" placeholder="{e(f["quick"]["placeholder"])}" '
            f'aria-label="{e(f["quick"]["label"])}"></div>')


def filter_empty(words: dict) -> str:
    """Shown by app.js when the chosen segment has no group on the page."""
    return f'<div class="none" data-none hidden>{e(words["empty"])}</div>'


# -- one task's drawer ----------------------------------------------------------------
def _find(patterns: list[str], tid: str, skip: tuple = ()):
    """The newest file under .review matching one of the patterns for this id, or None.
    skip: name endings that are never the file looked for (a report is not a task)."""
    root = snapshot.REVIEW
    for pat in patterns:
        try:
            hits = sorted(root.glob(pat.format(id=glob.escape(tid))), reverse=True)
        except (OSError, ValueError):
            continue
        for p in hits:
            try:
                if p.is_file() and not p.name.endswith(skip) and root.resolve() in p.resolve().parents:
                    return p
            except OSError:
                pass
    return None


def _read(p) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace") if p else ""
    except OSError:
        return ""


def report_fields(text: str, names: list[str]) -> dict[str, list[str]]:
    """A report's "NAME: value" lines and the bullets under them, by field name. Only the
    configured names start a field; any other line belongs to the field above it."""
    if not names:
        return {}
    head = re.compile("^(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r"):[ \t]*(.*)$")
    out, cur = {}, None
    for line in text.splitlines():
        m = head.match(line)
        if m:
            cur = m.group(1)
            out[cur] = [m.group(2).strip()] if m.group(2).strip() else []
        elif cur and line.strip():
            out[cur].append(re.sub(r"^\s*[-*]\s+", "", line).strip())
    return out


def _run_values(h: dict, labels: dict, now, c: dict) -> dict:
    w = c["words"]
    review = h.get("review") if isinstance(h.get("review"), dict) else {}
    end = board._dt(h.get("end"))
    try:
        took = w["took"].format(m=max(1, round(float(h.get("seconds") or 0) / 60))) if h.get("seconds") else ""
    except (TypeError, ValueError):
        took = ""
    return {"result": h.get("result") or "", "agent": labels.get(h.get("lane"), h.get("lane") or ""),
            "model": h.get("model") or "", "took": took, "spent": h.get("spent") or "",
            "finished": board._when(end, now, c["time_today"], c["time_other"]) if end else "",
            "gate": h.get("runner_gate") or "", "premise": h.get("premise") or "",
            "reviewer": review.get("verdict") or "",
            "reviewer_by": labels.get(review.get("lane"), review.get("lane") or ""),
            "reviewer_why": review.get("reasons") or review.get("why") or ""}


def task_detail(tid: str, now=None) -> tuple[bool, str]:
    """(found, html) for the drawer. The id is only ever matched against file names under
    .review (never joined into a path as given), and nothing is written."""
    c = _i()["drawer"]
    w = c["words"]
    live = conf.section("live")
    now = now or board.dt.datetime.now()
    missing = False, f'<div class="none">{e(w["not_found"])}</div>'
    if not isinstance(tid, str) or not tid or len(tid) > c["id_chars"] or any(s in tid for s in c["id_refuse"]):
        return missing
    not_task = tuple(c["files"]["not_task"])
    where = next(((p, t["status"]) for t in c["files"]["task"] for p in [_find([t["glob"]], tid, not_task)] if p), None)
    ts = (snapshot._state().get("tasks") or {}).get(tid)
    ts = ts if isinstance(ts, dict) else None
    if not where and not ts:
        return missing
    text = _read(where[0]) if where else ""
    title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), tid)
    labels = {a["lane"]: a["label"] for a in live["agents"]}
    cut = lambda s: board._cut(s, c["field_chars"])  # noqa: E731
    # the line under the title: where the task is, and the headers worth showing
    meta = [where[1]] if where else []
    for name in c["headers"]:
        m = re.search(rf"^{re.escape(name)}:[ \t]*(.+?)\s*$", text, re.M)
        if m:
            meta.append(w["header"].format(name=name, value=m.group(1)))
    hist = [h for h in (ts or {}).get("history") or [] if isinstance(h, dict)]
    v = _run_values(hist[-1], labels, now, c) if hist else {}
    badge = (f'<span class="why {e(live["timeline"]["classes"].get(v["result"], live["timeline"]["default_class"]))}">'
             f'{e(v["result"])}</span>') if v.get("result") else ""
    out = [f'<div class="date">{e(w["join"].join(meta))}</div><h2>{e(title)}{badge}</h2>']
    why = _read(_find(c["files"]["why"], tid)).strip().splitlines()
    if why:
        out.append(f'<section><h3>{e(w["parked"])}</h3><p>{e(cut(why[0]))}</p></section>')
    if v:
        rows = []
        for r in c["run_rows"]:
            vals = [cut(v[k]) for k in r["fields"] if v.get(k)]
            if vals:
                rows.append(f'<dt>{e(r["label"])}</dt><dd>{e(w["join"].join(vals))}</dd>')
        if rows:
            n = len(hist)
            out.append(f'<section><h3>{e(w["run"] if n == 1 else w["run_n"].format(n=n))}</h3>'
                       f'<dl>{"".join(rows)}</dl></section>')
    report = c["report"]
    fields = report_fields(_read(_find(c["files"]["report"], tid)), [f["name"] for f in report["fields"]])
    skip = {s.lower() for s in report["skip_values"]}
    body = []
    for f in report["fields"]:
        items = [i for i in fields.get(f["name"], []) if i.lower() not in skip]
        if f.get("hide") or not items:
            continue
        more = len(items) - report["max_items"]
        items = [cut(i) for i in items[:report["max_items"]]] + ([w["more"].format(n=more)] if more > 0 else [])
        body.append(f'<h4>{e(f["label"])}</h4>' + (f'<p>{e(items[0])}</p>' if len(items) == 1 else
                                                    f'<ul>{"".join(f"<li>{e(i)}</li>" for i in items)}</ul>'))
    if body:
        out.append(f'<section><h3>{e(w["report"])}</h3>{"".join(body)}</section>')
    elif hist:
        out.append(f'<section><h3>{e(w["report"])}</h3><div class="none">{e(w["no_report"])}</div></section>')
    asked = text.split("\n\n", 1)[1].strip() if "\n\n" in text else ""
    if asked:
        out.append(f'<section><h3>{e(w["task"])}</h3><p class="pre">{e(asked[:c["body_chars"]])}'
                   f'{e(w["cut"]) if len(asked) > c["body_chars"] else ""}</p></section>')
    if not hist:
        out.append(f'<section><div class="none">{e(w["no_run"])}</div></section>')
    return True, "".join(out)
