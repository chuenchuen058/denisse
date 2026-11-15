"""The live page's Do next card: the 3 things Chuen should do now.

    python -m atrium suggest --dry-run     print today's 3 suggestions, write nothing

1. gather(): facts from what is already on this laptop -- hot.md's waiting list, today's
   handoff and day note, Don's progress notes and mock-prep plans, the project plan files the
   config lists, and Orchestra's needs-you items. Each fact has a stable id (its file and, for
   a line, the line), the deadline found in it and whether he is behind on it.
2. rank(): the local model (local_llm: GPU lease, one num_ctx, a timeout) picks from the facts as
   strict JSON, validated here; Ollama down, the lease held, or a bad reply = rules(): soonest
   deadline, then behind, then oldest waiting, under the "rules, model busy" label.
3. pick(): his rules on every suggestion, the model's or the rules': day off (no study, only
   urgent items), study first inside his study slots, no investment advice, nothing sent as him;
   done and snoozed facts are left out.

The live page never waits on the model: current() returns the rules at once and starts one
background ranking, kept for suggest_refresh_minutes or until a fact changes. Building the
card writes nothing; only the Done / Snooze buttons do (state_file, and Done strikes the
hot.md line when the item came from hot.md and the line still matches exactly).
Every word, path, pattern, limit and rule: atrium/config.json "suggest" (defaults:
atrium/config_defaults.json).
"""
from __future__ import annotations

import datetime as dt
import glob as _glob
import hashlib
import html
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from . import conf, context, snapshot

DEFAULTS = Path(__file__).with_name("config_defaults.json")
_lock = threading.Lock()
_mem: dict = {}       # fp -> {"at", "mode", "ranked", "tried"}: the model's last answer per fact set
_running: set = set()  # fps a background ranking is working on
_last: dict = {}      # the facts behind the card last built: id -> fact (for the buttons)


# -- config -----------------------------------------------------------------------
def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _c() -> dict:
    try:
        base = json.loads(DEFAULTS.read_text(encoding="utf-8")).get("suggest", {})
    except (OSError, ValueError):
        base = {}
    return _merge(base, conf.load().get("suggest", {}))


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


# -- facts --------------------------------------------------------------------------
def _root(name: str) -> Path:
    return {"vault": context.VAULT, "repo": snapshot.REPO, "home": Path.home()}[name]


def _fid(*parts) -> str:
    return hashlib.sha1("\n".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def _body(text: str) -> str:
    """The note without its frontmatter."""
    m = re.match(r"^---\s*\n.*?\n---\s*(?:\n|$)", text, flags=re.S)
    return text[m.end():] if m else text


def _dates(text: str, today: dt.date, c: dict) -> list[dt.date]:
    d = c["dates"]
    months = [m.lower() for m in d["months"]]
    out = []
    for pat in d["patterns"]:
        for m in re.finditer(pat, text):
            g = m.groupdict()
            try:
                day = int(g["d"])
                if g.get("mon") is not None:
                    mon = g["mon"].lower()[:3]
                    if mon not in months:
                        continue
                    month = months.index(mon) + 1
                else:
                    month = int(g["m"])
                if g.get("y"):
                    out.append(dt.date(int(g["y"]), month, day))
                    continue
                when = dt.date(today.year, month, day)
                if (today - when).days > d["past_grace_days"]:
                    when = dt.date(today.year + 1, month, day)
                out.append(when)
            except (ValueError, KeyError, TypeError):
                continue
    return out


def _deadline(text: str, today: dt.date, c: dict) -> dt.date | None:
    ahead = [d for d in _dates(text, today, c) if d >= today]
    return min(ahead) if ahead else None


def _behind(text: str, c: dict) -> bool:
    return any(re.search(p, text) for p in c["behind_patterns"])


def _strip_between(text: str, pair) -> str:
    if not pair:
        return text
    return re.sub(re.escape(pair[0]) + r".*?" + re.escape(pair[1]), "", text, flags=re.S)


def _lines(text: str) -> list[str]:
    return [l for l in context._bullets(text).splitlines() if l.strip()]


def _sections(text: str) -> list[tuple[str, str]]:
    return [(m.group(1).strip(), m.group(2)) for m in
            re.finditer(r"^##+ (.+?)\s*\n(.*?)(?=^##+ |\Z)", text, flags=re.M | re.S)]


def _cut(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _files(src: dict, today: str) -> list[tuple[Path, str]]:
    """(path, rel) of every file the source's glob matches, sorted."""
    root = _root(src.get("root", "vault"))
    pat = src["glob"].format(today=today)
    out = []
    for p in sorted(_glob.glob(str(root / pat), recursive=True)):
        path = Path(p)
        if path.is_file():
            out.append((path, path.relative_to(root).as_posix()))
    return out


def _fact(c, src, path, rel, title, text, today, *, order, line="", deadline=None) -> dict:
    text = _cut(text, c["fact_chars"])
    return {"id": _fid(src["id"], rel, line or title), "source": src["id"],
            "label": src.get("label", src["id"]).format(stem=path.stem if path else "", today=today.isoformat()),
            "root": src.get("root", "vault"), "rel": rel, "kind": src.get("kind", c["kinds"][0]),
            "title": title, "text": text, "line": line if src.get("strike") else "",
            "deadline": (deadline or _deadline(text, today, c)), "behind": _behind(text, c),
            "rank": src.get("rank", True), "order": order}


def gather(now: dt.datetime, needs: list[dict] | None = None) -> list[dict]:
    """Every fact the sources and Orchestra give, in source order (= oldest first within one)."""
    c = _c()
    today = now.date()
    facts: list[dict] = []
    for src in c["sources"]:
        try:
            files = _files(src, today.isoformat())
        except (KeyError, OSError, ValueError):
            continue
        for path, rel in files:
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            body = _strip_between(_body(raw), src.get("skip_between"))
            mode = src.get("mode", "lines")
            name = re.sub(src.get("name_strip") or r"(?!)", "", path.stem).strip()
            if mode == "lines":
                part = context._section(body, src["section"]) if src.get("section") else body
                for l in _lines(part):
                    if re.match(c["struck"], l):
                        continue
                    text = re.sub(r"^\s*[-*]\s*", "", l).strip()
                    if text:
                        facts.append(_fact(c, src, path, rel, text, text, today, order=len(facts), line=l.rstrip()))
            elif mode == "note":
                parts = [context._section(body, s) for s in src.get("sections") or []]
                text = "\n".join(p for p in parts if p) or body
                if src.get("last_lines"):
                    text = "\n".join([l for l in text.splitlines() if l.strip()][-src["last_lines"]:])
                text = text.strip()
                if text:
                    facts.append(_fact(c, src, path, rel, src.get("title", "{name}").format(name=name, stem=path.stem),
                                       text, today, order=len(facts)))
            elif mode == "sections":
                upcoming = []
                for heading, sec in _sections(body):
                    ends = _dates(heading, today, c)
                    if ends and max(ends) >= today:
                        upcoming.append((heading, sec, max(ends)))
                for heading, sec, end in upcoming[:1]:
                    facts.append(_fact(c, src, path, rel, src.get("title", "{heading}").format(heading=heading, name=name),
                                       heading + "\n" + sec, today, order=len(facts), deadline=end))
    o = c.get("orchestra") or {}
    if o.get("use"):
        src = {"id": o["id"], "label": o["label"], "kind": o["kind"], "root": "repo"}
        for n in needs if needs is not None else _needs():
            text = str(n.get("item") or "").strip()
            if text:
                facts.append(_fact(c, src, None, "", text, text, today, order=len(facts)))
    return facts


def _needs() -> list[dict]:
    """Orchestra's needs-you items (drafts to approve, parked tasks), as the hub lists them."""
    try:
        t = snapshot.tables(["tasks"])
        return [w for w in snapshot.waiting_items(t["tasks"]) if w.get("kind") != "note"]
    except Exception:  # noqa: BLE001 -- no Orchestra state: nothing from it
        return []


def fingerprint(facts: list[dict], now: dt.datetime) -> str:
    return _fid(now.date().isoformat(), *[f["id"] + f["text"] for f in facts])


# -- his rules --------------------------------------------------------------------------
def _hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def day_off(now: dt.datetime, c: dict | None = None) -> bool:
    c = c or _c()
    return now.strftime("%A") in c["days_off"]


def in_slot(now: dt.datetime, c: dict | None = None) -> bool:
    c = c or _c()
    t = now.time()
    return any(_hm(s["from"]) <= t < _hm(s["to"]) for s in c["study_slots"])


def guard(item: dict, c: dict) -> dict | None:
    """The item with his never-rules applied: None = never shown; a send = a draft."""
    for g in c["guard"]:
        text = f'{item["title"]} {item.get("why", "")}'
        if not re.search(g["pattern"], text):
            continue
        if g["action"] == "drop":
            return None
        if g["action"] == "draft" and not (g.get("unless") and re.search(g["unless"], item["title"])):
            item = dict(item, title=c["draft_template"].format(title=item["title"]))
    return item


# -- ranking ----------------------------------------------------------------------------
def _minutes(kind: str, c: dict, asked=None) -> int:
    m = c["minutes"]
    n = asked if isinstance(asked, int) and not isinstance(asked, bool) else m["by_kind"].get(kind, m["min"])
    return max(m["min"], min(m["max"], n))


def _why(f: dict, today: dt.date, c: dict) -> str:
    w = c["why"]
    if f["deadline"]:
        return w["deadline"].format(date=f["deadline"].strftime(w["date"]), days=(f["deadline"] - today).days)
    if f["behind"]:
        return w["behind"]
    return w["waiting"].format(source=f["label"])


def rules(facts: list[dict], now: dt.datetime, c: dict | None = None) -> list[dict]:
    """The plain ranking: soonest deadline, then (in a study slot) study, then behind, then the
    oldest waiting (source order). Every rankable fact, best first."""
    c = c or _c()
    slot = in_slot(now, c)
    far = dt.date.max
    ranked = sorted((f for f in facts if f["rank"]),
                    key=lambda f: (f["deadline"] or far, 0 if slot and f["kind"] == c["study_kind"] else 1,
                                   0 if f["behind"] else 1, f["order"]))
    return [{"fact": f["id"], "title": f["title"], "why": _why(f, now.date(), c),
             "minutes": _minutes(f["kind"], c), "kind": f["kind"]} for f in ranked]


def prompt(facts: list[dict], now: dt.datetime, c: dict) -> str:
    m = c["model"]
    lines, used = [], 0
    for f in facts:
        line = json.dumps({"id": f["id"], "from": f["label"], "kind": f["kind"],
                           "due": f["deadline"].isoformat() if f["deadline"] else None, "text": f["text"]},
                          ensure_ascii=False)
        if used + len(line) > c["facts_chars"]:
            break
        lines.append(line)
        used += len(line)
    slots = ", ".join(f'{s["from"]}-{s["to"]}' for s in c["study_slots"])
    return "\n".join(m["prompt"]).format(now=now.strftime("%H:%M"), weekday=now.strftime("%A %d %B %Y"),
                                         day_off=day_off(now, c), in_slot=in_slot(now, c), slots=slots,
                                         kinds=", ".join(c["kinds"]), n=c["ask_count"], facts="\n".join(lines))


def parse(reply, facts: list[dict], c: dict) -> list[dict]:
    """The model's reply as ranked items; raises ValueError on anything that isn't the shape asked
    for (not JSON, no items, a field of the wrong type, a source not among the facts, a kind not
    in kinds). One bad item fails the whole reply: a model that half-follows is not trusted."""
    data = json.loads(reply) if isinstance(reply, str) else reply
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) or not data["items"]:
        raise ValueError("model reply: no items")
    ids = {f["id"] for f in facts}
    example = c["model"]["example"]["items"][0]
    out = []
    for it in data["items"]:
        if not isinstance(it, dict):
            raise ValueError("model reply: an item is not an object")
        for k, v in example.items():
            if not isinstance(it.get(k), type(v)) or isinstance(it.get(k), bool):
                raise ValueError(f"model reply: {k} is not a {type(v).__name__}")
        if it["source"] not in ids:
            raise ValueError(f"model reply: unknown source {it['source']!r}")
        if it["kind"] not in c["kinds"]:
            raise ValueError(f"model reply: unknown kind {it['kind']!r}")
        if not it["title"].strip():
            raise ValueError("model reply: empty title")
        out.append({"fact": it["source"], "title": it["title"].strip(), "why": it["why"].strip(),
                    "minutes": _minutes(it["kind"], c, it["minutes"]), "kind": it["kind"]})
    return out


def ask_model(text: str, c: dict) -> str:
    """One call to the local model, through local_llm (the GPU lease: a held lease raises
    LeaseBusy). Returns the reply's text. Raises on any failure."""
    if str(snapshot.REPO) not in sys.path:
        sys.path.insert(0, str(snapshot.REPO))
    import local_llm
    import model_routing
    import ollama_output
    m = c["model"]
    model = model_routing.model_for(m["job"], entry=m["entry"])
    body = {"model": model, "stream": False, "format": ollama_output.schema_from_example(m["example"]),
            "messages": [{"role": "user", "content": text}], "options": m["options"]}
    data = local_llm.http(None, m["path"], body, job=m["job"], timeout=float(m["timeout_s"]))
    return (data.get("message") or {}).get("content") or ""


def rank(facts: list[dict], now: dt.datetime, ask=None, c: dict | None = None) -> tuple[str, list[dict]]:
    """("model", items) from the model, or ("rules", items) when it can't answer or answers badly."""
    c = c or _c()
    ask = ask or ask_model
    if not any(f["rank"] for f in facts):
        return "rules", []
    try:
        return "model", parse(ask(prompt(facts, now, c), c), facts, c)
    except Exception:  # noqa: BLE001 -- down, lease held, timeout, bad JSON: the rules ranking
        return "rules", rules(facts, now, c)


# -- done / snooze state ------------------------------------------------------------------
def _state_path() -> Path:
    return snapshot.REVIEW / _c()["state_file"]


def load_state(now: dt.datetime) -> dict:
    """{"day", "done": {id: when}, "snoozed": {id: until}}; a new day starts empty."""
    day = now.date().isoformat()
    try:
        s = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    if not isinstance(s, dict) or s.get("day") != day:
        s = {"day": day}
    s.setdefault("done", {}), s.setdefault("snoozed", {})
    return s


def _save_state(s: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def hidden(state: dict, now: dt.datetime) -> set:
    out = set(state.get("done", {}))
    for fid, until in state.get("snoozed", {}).items():
        try:
            if dt.datetime.fromisoformat(until) > now:
                out.add(fid)
        except (TypeError, ValueError):
            pass
    return out


def strike(fact: dict, now: dt.datetime) -> bool:
    """Strike the fact's line in its note, only when the note still holds exactly that line."""
    if not fact.get("line"):
        return False
    c = _c()
    p = _root(fact["root"]) / fact["rel"]
    try:
        raw = p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    lines = raw.splitlines(keepends=True)
    for i, l in enumerate(lines):
        body = l.rstrip("\r\n")
        if body != fact["line"]:
            continue
        m = re.match(r"^(\s*[-*]\s*)(.*)$", body)
        if not m:
            return False
        s = c["strike"]
        when = s["date"].format(d=now.day, mon=now.strftime("%b"))
        lines[i] = s["template"].format(lead=m.group(1), text=m.group(2), date=when) + l[len(body):]
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes("".join(lines).encode("utf-8"))
        os.replace(tmp, p)
        return True
    return False


def act(action: str, fid: str, now: dt.datetime, facts: dict | None = None) -> dict:
    """Done or snooze one fact. facts: id -> fact (default: the card last built). Done strikes
    its hot.md line when it came from there and still matches; either way it is recorded."""
    c = _c()
    facts = _last if facts is None else facts
    if action not in ("done", "snooze") or not isinstance(fid, str) or not fid:
        raise ValueError("action is done or snooze, with an id")
    s = load_state(now)
    out = {"ok": True, "id": fid, "action": action, "struck": False}
    if action == "done":
        s["done"][fid] = now.isoformat(timespec="seconds")
        f = facts.get(fid)
        out["struck"] = bool(f) and strike(f, now)
    else:
        s["snoozed"][fid] = (now + dt.timedelta(minutes=c["snooze_minutes"])).isoformat(timespec="seconds")
    _save_state(s)
    return out


# -- choosing the 3 -------------------------------------------------------------------------
def pick(ranked: list[dict], facts: list[dict], now: dt.datetime, state: dict, c: dict | None = None,
         fill: list[dict] | None = None) -> list[dict]:
    """The first count items his rules allow, from ranked then fill; one per fact."""
    c = c or _c()
    by = {f["id"]: f for f in facts}
    off, gone = day_off(now, c), hidden(state, now)
    urgent = now.date() + dt.timedelta(days=c["urgent_days"])
    out, seen = [], set()
    for it in [*ranked, *(fill or [])]:
        f = by.get(it["fact"])
        if not f or f["id"] in seen or f["id"] in gone:
            continue
        if off and (it["kind"] == c["study_kind"] or f["kind"] == c["study_kind"]
                    or not (f["deadline"] and f["deadline"] <= urgent)):
            continue
        g = guard(it, c)
        if g is None:
            continue
        seen.add(f["id"])
        out.append(dict(g, id=f["id"], source=f["label"], link=link(f), line=f["line"]))
        if len(out) >= c["count"]:
            break
    return out


def link(f: dict) -> str:
    c = _c()
    if not f.get("rel"):
        return ""
    if f["root"] == "vault":
        o = c["obsidian"]
        vault = o.get("vault_name") or context.VAULT.name
        rel = f["rel"][:-3] if f["rel"].endswith(".md") else f["rel"]
        return o["uri"].format(vault=urllib.parse.quote(vault, safe=""), file=urllib.parse.quote(rel, safe=""))
    return c["file_uri"].format(path=urllib.parse.quote((_root(f["root"]) / f["rel"]).as_posix(), safe="/:"))


# -- what the card shows ------------------------------------------------------------------
def _remember(facts: list[dict]) -> None:
    global _last
    _last = {f["id"]: f for f in facts}


def _background(fp: str, facts: list[dict], now: dt.datetime, ask) -> None:
    try:
        mode, ranked = rank(facts, now, ask)
        with _lock:
            _mem[fp] = {"at": time.monotonic(), "mode": mode, "ranked": ranked, "tried": time.monotonic()}
    finally:
        with _lock:
            _running.discard(fp)


def current(now: dt.datetime, needs: list[dict] | None = None, *, model_up: bool = True, ask=None,
            wait: bool = False) -> dict:
    """The card's data: {"mode", "items", "facts", "day_off"}. The model's ranking for these facts
    when one is fresh; else the rules now, and (model_up, model enabled) one background ranking,
    or with wait=True the model's answer before returning (the CLI)."""
    c = _c()
    facts = gather(now, needs)
    _remember(facts)
    fp = fingerprint(facts, now)
    state = load_state(now)
    base = rules(facts, now, c)
    with _lock:
        hit = _mem.get(fp)
        fresh = bool(hit and hit["mode"] == "model"
                     and time.monotonic() - hit["at"] < c["suggest_refresh_minutes"] * 60)
        retry = not hit or time.monotonic() - hit["tried"] >= (
            c["suggest_refresh_minutes"] if hit["mode"] == "model" else c["busy_retry_minutes"]) * 60
        start = (not fresh and retry and model_up and c["model"]["enabled"] and fp not in _running
                 and any(f["rank"] for f in facts))
        if start:
            _running.add(fp)
    if start:
        if wait:
            _background(fp, facts, now, ask)
        else:
            threading.Thread(target=_background, args=(fp, facts, now, ask), daemon=True).start()
    with _lock:
        hit = _mem.get(fp)
    mode, ranked = (hit["mode"], hit["ranked"]) if hit and hit["mode"] == "model" else ("rules", base)
    items = pick(ranked, facts, now, state, c, fill=base)
    return {"mode": mode, "items": items, "facts": facts, "day_off": day_off(now, c)}


def card(data: dict) -> str:
    """The card's HTML (v3 mission-board style): three cards, each title, why, time pill, source
    link, Done and Snooze; the mode; a "why these?" list of the facts the ranking was given."""
    c = _c()
    w = c["words"]
    mode = w["mode_model"] if data["mode"] == "model" else w["mode_rules"]
    url = _e(c["http_path"])
    items = []
    for it in data["items"]:
        src = (f'<a class="src" href="{_e(it["link"])}">{_e(it["source"])}</a>' if it["link"]
               else f'<span class="src">{_e(it["source"])}</span>')
        items.append(
            f'<li class="sg {_e(it["kind"])}" data-id="{_e(it["id"])}"><b class="t">{_e(it["title"])}</b>'
            f'<span class="why">{_e(it["why"])}</span>'
            f'<span class="meta"><span class="pill">{_e(w["minutes"].format(n=it["minutes"]))}</span>{src}</span>'
            f'<span class="acts"><button type="button" data-suggest="done" data-id="{_e(it["id"])}" data-url="{url}">'
            f'{_e(w["done"])}</button><button type="button" data-suggest="snooze" data-id="{_e(it["id"])}" '
            f'data-url="{url}">{_e(w["snooze"])}</button></span></li>')
    if items:
        body = f'<ul class="do-next">{"".join(items)}</ul>'
    else:
        body = f'<div class="none">{_e(w["day_off"] if data["day_off"] else w["none"])}</div>'
    given = "".join(f'<li><b>{_e(f["label"])}</b> {_e(_cut(f["text"], 160))}</li>' for f in data["facts"])
    facts = (f'<details class="why-these"><summary>{_e(w["why_toggle"])}</summary>'
             f'<ul>{given or "<li>" + _e(w["no_facts"]) + "</li>"}</ul></details>')
    return f'<span class="sg-mode">{_e(mode)}</span>{body}{facts}'


def show(data: dict) -> str:
    """The suggestions as plain text, for the terminal."""
    c = _c()
    w = c["words"]
    out = [w["mode_model"] if data["mode"] == "model" else w["mode_rules"]]
    for n, it in enumerate(data["items"], 1):
        out.append(f'{n}. {it["title"]} ({w["minutes"].format(n=it["minutes"])}, {it["kind"]}, {it["source"]})')
        out.append(f'   {it["why"]}')
    if not data["items"]:
        out.append(w["day_off"] if data["day_off"] else w["none"])
    return "\n".join(out)


def main(dry_run: bool = True) -> int:
    """python -m atrium suggest [--dry-run]: today's suggestions, the model asked and waited for
    when Ollama answers. Writes nothing (the card's buttons are the only writers)."""
    sys.stdout.reconfigure(encoding="utf-8")
    from . import board
    up = board._ollama() is not None
    print(show(current(dt.datetime.now(), model_up=up, wait=True)))
    return 0
