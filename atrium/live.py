"""The live Atrium page, served on this laptop: python -m atrium live [--port N] [--open]

Reads the same tables `atrium snapshot` builds (snapshot.tables, nothing collected
twice), shapes them into the page's rows (agents + needs you, where each agent is working and
who is about to collide (atrium/cursors.py), today's timeline, progress) and pushes a new copy down a server-sent event stream whenever a watched file
changes. Binds loopback only. Read-only: every handler is a GET, nothing is written, and
nothing runs while no tab holds the stream open. Every port, interval, path, label and
colour: atrium/config.json "live".
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import conf, context, cursors, snapshot

HERE = Path(__file__).resolve().parent


def _c() -> dict:
    return conf.section("live")


def _review() -> Path:
    return snapshot.REVIEW  # read at call time, so a test's temp .review is the one served


# -- the page's data -------------------------------------------------------------
def _hours(ts: dt.datetime, midnight: dt.datetime) -> float:
    return max(0.0, (ts - midnight).total_seconds() / 3600)


def _iso(s) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(s))
    except ValueError:
        return None


def _titles() -> dict:
    """task id -> title, from the task files (queue, parked, archive)."""
    r = _review()
    out = {}
    for p in [*r.glob("queue/*.md"), *r.glob("parked/*.md"), *r.glob("archive/*/*.md")]:
        if not p.name.endswith(".report.md"):
            try:
                out[p.stem] = snapshot._title(p, p.stem)
            except OSError:
                pass
    return out


def _timeline(state: dict, running: list[dict], now: dt.datetime) -> list[dict]:
    """Today's runs as blocks: start/end in hours since midnight, the result's class."""
    c = _c()["timeline"]
    n = snapshot._c()["title_chars"]
    midnight = dt.datetime.combine(now.date(), dt.time())
    titles = _titles()
    rows = []
    for slug, ts in state.get("tasks", {}).items():
        for h in ts.get("history", []):
            end = _iso(h.get("end", ""))
            if not end or end < midnight:
                continue
            start = end - dt.timedelta(seconds=h.get("seconds", 0) or 0)
            rows.append({"lane": h.get("lane", "?"), "start": round(_hours(start, midnight), 3),
                         "end": round(_hours(end, midnight), 3), "result": h.get("result") or "",
                         "cls": c["classes"].get(h.get("result"), c["default_class"]),
                         "task": titles.get(slug, slug)[:n]})
    for r in running:
        if not r["task"]:
            continue
        since = _iso(state.get("tasks", {}).get(r["id"], {}).get("last_start") or "")
        if since is None:
            since = now - dt.timedelta(minutes=r["minutes"])
        rows.append({"lane": r["lane"], "start": round(_hours(since, midnight), 3), "end": None,
                     "result": "", "cls": "run", "task": r["task"]})
    rows.sort(key=lambda b: b["start"])
    return rows


def _note_status(note: Path) -> bool | None:
    """Status from the week note itself, if present; a missing or undecodable status stays unknown."""
    try:
        text = note.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    def parse(value: str | None) -> bool | None:
        if value is None:
            return None
        value = value.strip().strip("\"'")
        if not value:
            return None
        lower = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
        if any(token in lower for token in ("done", "complete", "finished", "written", "checked", "ready")):
            return True
        if any(token in lower for token in ("not done", "pending", "todo", "draft", "partial", "blocked", "unknown", "in progress")):
            return False
        return None

    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, flags=re.DOTALL)
    if m:
        for status_re in (r"(?m)^\s*status\s*:\s*(.+?)\s*$", r"(?m)^\s*Status\s*:\s*(.+?)\s*$"):
            sm = re.search(status_re, m.group(1))
            if sm:
                outcome = parse(sm.group(1))
                if outcome is not None:
                    return outcome
    match = re.search(r"(?im)^\s*(?:status|Status)\s*[:=]\s*(.+?)\s*$", text)
    if match:
        outcome = parse(match.group(1))
        if outcome is not None:
            return outcome
    return None


def _study_weeks() -> dict:
    """Week notes written of weeks filed, from the vault's module folders."""
    c = _c()["study"]
    root = context.VAULT / c["root"]
    filed = written = 0
    try:
        weeks = [w for m in root.iterdir() if m.is_dir() for w in m.glob(c["week_glob"]) if w.is_dir()]
    except OSError:
        weeks = []
    for w in weeks:
        filed += 1
        status = None
        for note in w.glob(c["note_glob"]):
            status = _note_status(note)
            if status is not None:
                break
        if status is True:
            written += 1
            continue
        if status is None:
            for note in w.glob(c["note_glob"]):
                try:
                    if len(note.read_text(encoding="utf-8", errors="replace")) >= c["written_min_chars"]:
                        written += 1
                        break
                except OSError:
                    pass
    return {"v": written, "m": filed}


def _power() -> dict:
    if str(snapshot.REPO) not in sys.path:
        sys.path.insert(0, str(snapshot.REPO))
    try:
        from orchestra import power
        return power.power_status()
    except Exception:  # noqa: BLE001 -- unknown power is shown as nothing, never an error
        return {"ac": None, "battery_percent": None}


def collect() -> dict:
    """Everything the page shows, as one JSON-able dict (no clock in it: see payload())."""
    c = _c()
    words = c["words"]
    now = dt.datetime.now()
    t = snapshot.tables(c["tables"])
    state = snapshot._state()
    pw = _power()
    usage = {u["lane"]: u for u in t["usage"]}
    lanes = state.get("lanes") if isinstance(state.get("lanes"), dict) else {}
    limits = {lane: _iso(v.get("limited_until") or "") for lane, v in lanes.items() if isinstance(v, dict)}
    queued: dict[str, int] = {}
    held = {r["id"] for r in t["running"] if r["task"]}  # a running task's file stays in queue/
    for r in t["tasks"]:
        if r["status"] == "queued" and r["id"] not in held:
            queued[r["lane"]] = queued.get(r["lane"], 0) + 1
    agents = []
    for a in c["agents"]:
        u = usage.get(a["lane"], {})
        busy, slots = u.get("busy", 0), u.get("slots", 0)
        lim = limits.get(a["lane"])
        limited = u.get("state") == "bad" or bool(lim and lim > now)
        n = queued.get(a["lane"], 0)
        why = (words["why_limited"] if limited else words["why_full"] if slots and busy >= slots
               else words["why_battery"] if pw.get("ac") is False else words["why_none"]) if n else ""
        if lim and lim > now and u.get("state") != "bad":  # only the runner's record knows this limit
            status = words["until"].format(time=lim.strftime(words["until_format"]))
        else:
            status = (u.get("status") or "") if limited else words["working"] if busy else words["free"]
        agents.append({"lane": a["lane"], "label": a["label"], "colour": a["colour"],
                       "fill": round(100 * busy / slots) if busy and slots else (100 if busy else 0),
                       "status": status, "state": "bad" if limited else "busy" if busy else "ok",
                       "queued": words["queued"].format(n=n) if n else "",
                       "held": words["held"] if n and limited else "", "why": why,
                       "until": lim.isoformat(timespec="minutes") if lim and lim > now else ""})
    icons = c["needs_icons"]
    where = dict(cursors.collect(t["running"], agents, c["refresh_s"], words), icon=icons.get("overlap", ""))
    needs = [{"icon": icons.get("overlap", ""), "item": o["text"], "kind": "overlap"} for o in where["overlaps"]]
    needs += [{"icon": icons.get(w["kind"], ""), "item": w["item"], "kind": w["kind"]}
              for w in snapshot.waiting_items(t["tasks"])]
    needs = needs[:c["needs_max"]]
    timeline = _timeline(state, t["running"], now)
    today = now.date().isoformat()
    done_res = set(c["progress"]["done_results"])
    touched, done = set(), set()
    for slug, ts in state.get("tasks", {}).items():
        for h in ts.get("history", []):
            if str(h.get("end", "")).startswith(today):
                touched.add(slug)
                if h.get("result") in done_res:
                    done.add(slug)
    touched |= {r["id"] for r in t["running"] if r["task"]}
    touched |= {r["id"] for r in t["tasks"] if r["status"] == "queued"}
    spend = (t["spend"] or [{}])[0]
    values = {"tasks": {"v": len(done), "m": len(touched)},
              "study": _study_weeks(),
              "spend": {"v": spend.get("cost_usd", 0) or 0, "m": spend.get("cap_usd", 0) or 0}}
    progress = []
    for b in c["progress"]["bars"]:
        v = values.get(b["key"], {"v": 0, "m": 0})
        progress.append({"key": b["key"], "label": b["label"], "colour": b["colour"],
                         "pct": round(min(100, 100 * v["v"] / v["m"]), 1) if v["m"] else 0,
                         "text": b["text"].format(**v)})
    return {"title": c["title"], "subtitle": c["subtitle"], "panels": c["panels"], "headings": c["headings"],
            "power": {"ac": pw.get("ac"), "battery_percent": pw.get("battery_percent")},
            "agents": agents, "needs": needs, "timeline": timeline, "progress": progress, "where": where,
            "timeline_cfg": {k: c["timeline"][k] for k in ("start_hour", "end_hour", "pad_hours", "tick_hours")},
            "words": words}


def payload(data: dict, version: int) -> str:
    now = dt.datetime.now()
    midnight = dt.datetime.combine(now.date(), dt.time())
    return json.dumps(dict(data, version=version, now_h=round(_hours(now, midnight), 4),
                           day=now.date().isoformat()), ensure_ascii=False)


# -- noticing a change -----------------------------------------------------------
def fingerprint() -> tuple:
    """Size and mtime of every watched file (a folder: its entries and theirs). Stat only."""
    w = _c()["watch"]
    seen = []

    def stat(p: Path):
        try:
            s = p.stat()
            seen.append((str(p), s.st_mtime_ns, s.st_size))
        except OSError:
            pass

    r = _review()
    for pat in w["review"]:
        for p in sorted(r.glob(pat)):
            stat(p)
            if p.is_dir():
                for q in sorted(p.iterdir()):
                    stat(q)
                    if q.is_dir():
                        for x in sorted(q.iterdir()):
                            stat(x)
    for rel in w["vault"]:
        stat(context.VAULT / rel)
    return tuple(seen)


class Hub:
    """One cached copy of the page's data for every connected tab. Rebuilt only when asked
    (that is, while a tab is connected) and the files changed or refresh_s passed."""

    def __init__(self):
        self.lock = threading.Lock()
        self.fp = None
        self.data = None
        self.body = None
        self.version = 0
        self.at = 0.0

    def get(self) -> tuple[int, dict]:
        with self.lock:
            fp = fingerprint()
            if self.data is None or fp != self.fp or time.monotonic() - self.at >= _c()["refresh_s"]:
                data = collect()
                body = json.dumps(data, sort_keys=True, ensure_ascii=False)
                if body != self.body:
                    self.version += 1
                    self.data, self.body = data, body
                self.fp, self.at = fp, time.monotonic()
            return self.version, self.data


# -- the server -----------------------------------------------------------------
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    hub: Hub = None  # set by make_server
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # pythonw has no console; keep stdout quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/events":
            return self._events()
        if path == "/data":
            v, data = self.hub.get()
            return self._send(200, payload(data, v).encode("utf-8"), "application/json; charset=utf-8")
        page = HERE / _c()["page_dir"]
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (page / name).resolve()
        if f.parent != page.resolve() or not f.is_file() or f.suffix not in TYPES:
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        self._send(200, f.read_bytes(), TYPES[f.suffix])

    def _refuse(self):
        self._send(405, b"read-only", "text/plain; charset=utf-8")

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

    def _events(self):
        c = _c()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        sent, quiet = -1, 0.0
        try:
            while not self._gone():
                v, data = self.hub.get()
                if v != sent:
                    self.wfile.write(f"event: update\ndata: {payload(data, v)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    sent, quiet = v, 0.0
                elif quiet >= c["keepalive_s"]:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    quiet = 0.0
                if self.server.stopping.wait(c["poll_s"]):
                    return
                quiet += c["poll_s"]
        except (BrokenPipeError, ConnectionError, OSError):
            return  # the tab went away: this thread ends and nothing runs for it

    def _gone(self) -> bool:
        """The tab closed its end (an event stream's client never sends after the request,
        so anything readable is the close). Checked every poll, so a closed tab costs nothing."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)
        except OSError:
            return True


class LiveServer(ThreadingHTTPServer):
    """Closing it ends every open event stream (and waits for them), so nothing polls on."""
    daemon_threads = False
    block_on_close = True

    def __init__(self, *a, **k):
        self.stopping = threading.Event()
        super().__init__(*a, **k)

    def server_close(self):
        self.stopping.set()
        super().server_close()


def _port_busy(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return True
        return False


def make_server(host: str | None = None, port: int | None = None) -> "LiveServer":
    c = _c()
    host = host or c["host"]
    if host not in c["loopback"]:
        raise ValueError(f"atrium live binds loopback only, not {host!r}")
    chosen = c["port"] if port is None else port
    if chosen is not None and chosen != 0 and _port_busy(host, chosen):
        if port is None:
            for candidate in range(chosen + 1, chosen + 128):
                if not _port_busy(host, candidate):
                    chosen = candidate
                    break
            else:
                raise OSError(f"address in use on loopback: {host}:{chosen}")
        else:
            raise OSError(f"address in use on loopback: {host}:{chosen}")
    handler = type("LiveHandler", (Handler,), {"hub": Hub()})
    return LiveServer((host, chosen), handler)


def url(port: int | None = None) -> str:
    c = _c()
    return f"http://{c['host']}:{c['port'] if port is None else port}/"


def serve(port: int | None = None, open_browser: bool = False, browser: str | None = None) -> int:
    srv = make_server(port=port)
    where = url(srv.server_address[1])
    print(where, flush=True)
    if open_browser:
        import webbrowser
        target = browser or conf.section("install_live").get("browser")
        if target:
            cmd = os.path.expandvars(os.path.expanduser(str(target)))
            webbrowser.register("atrium-browser", None, webbrowser.BackgroundBrowser(cmd))
            webbrowser.get("atrium-browser").open(where)
        else:
            webbrowser.open(where)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
