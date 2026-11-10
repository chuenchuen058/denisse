"""The local model learns from Claude's corrections (Chuen, 10 Nov 2026: "I
want to continue making qwen, all the local agents better").

When a local answer fails its check and Claude's answer is taken instead
(teach's self-check, a consult the local model could not answer, a card the
checker corrected), the pair is kept:

    capture(paths, job, input, local, why, accepted)   one correction into the
                                     private store (Don's data area, outside
                                     the public repo; tutor.json learn.store_file)
    shots(paths, job, query)         the 1-3 most similar accepted examples for
                                     this job, as a prompt block within the
                                     budget -- inserted before a local call
    report(paths, days) / render_report(...)
                                     the top failure reasons per job and model,
                                     so the prompts can be fixed

Similarity is cosine over Ollama embeddings when tutor.json learn.embed.model
names one (and it answers), else word overlap -- no GPU at all. Nothing here
ever stops the tutor: a store that can't be read or written is a log line.
Every sentence, limit and file name is tutor.json "learn".
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import math
import os

from . import topics
from .conf import CONFIG, Paths

_L = CONFIG["learn"]
log = logging.getLogger("denisse.tutor")


# -- the store ------------------------------------------------------------------

def store_path(paths: Paths) -> str:
    return paths.data(_L["store_file"])


def load(paths: Paths, job: str | None = None) -> list:
    """Every record (oldest first), or one job's. A missing or broken store is empty."""
    path = store_path(paths)
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and (job is None or rec.get("job") == job):
                    out.append(rec)
    except OSError:
        return []
    return out


def _text(value, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return " ".join(value.split())[:limit]


def local_model() -> str:
    """The local model's name as config.json has it now ('' if unknown)."""
    try:
        import model_config
        return model_config.model(CONFIG["model"]["config_entry"])
    except Exception:  # noqa: BLE001 -- a record without a model name is still a record
        return ""


def _write(paths: Paths, records: list) -> None:
    path = store_path(paths)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _trim(records: list) -> list:
    """The newest keep_per_job records of each job, in their order."""
    keep = _L["keep_per_job"]
    counts = collections.Counter()
    out = []
    for rec in reversed(records):
        counts[rec.get("job")] += 1
        if counts[rec.get("job")] <= keep:
            out.append(rec)
    return list(reversed(out))


def capture(paths: Paths, job: str, input_text, local_output, why: str, accepted, *, model: str | None = None,
            engine: str = "", module: str = "", when: str | None = None, embed=None) -> dict | None:
    """Keep one correction: what was asked, what the local model said, why it
    did not stand, and the answer that was accepted instead (None when the
    fallback gave none -- still counted in the report, never a few-shot).
    Returns the record, or None when learning is off or the store failed."""
    if not _L["enabled"]:
        return None
    rec = {"when": when or dt.datetime.now().isoformat(timespec="seconds"), "job": job,
           "model": local_model() if model is None else model, "engine": engine, "module": module,
           "input": _text(input_text, _L["input_chars"]), "local": _text(local_output, _L["output_chars"]),
           "why": _text(why, _L["why_chars"]),
           "accepted": None if accepted is None else _text(accepted, _L["output_chars"])}
    vec = _embed(rec["input"], embed)
    if vec is not None:
        rec["vec"] = [round(x, _L["embed"]["digits"]) for x in vec]
        rec["vec_model"] = _L["embed"]["model"]
    try:
        _write(paths, _trim(load(paths) + [rec]))
    except OSError as exc:
        log.info(_L["say_store_failed"].format(why=exc))
        return None
    return rec


# -- similarity ------------------------------------------------------------------

def _embed(text: str, embed=None):
    """The text's embedding, or None (no embed model set, or it failed).
    `embed` (tests) is a function text -> list of floats."""
    if embed is None:
        name = _L["embed"]["model"]
        if not name:
            return None

        def embed(t):
            import ollama
            r = ollama.embed(model=name, input=t, keep_alive=_L["embed"]["keep_alive"])
            vecs = r["embeddings"] if isinstance(r, dict) else getattr(r, "embeddings", None)
            return list(vecs[0])
    try:
        vec = embed(text)
        return [float(x) for x in vec] if vec else None
    except Exception as exc:  # noqa: BLE001 -- no embeddings means word overlap, never a failed call
        log.info(_L["say_embed_failed"].format(why=exc))
        return None


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _overlap(a: str, b: str) -> float:
    wa, wb = topics._words(a), topics._words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def similar(records: list, job: str, query: str, embed=None, k: int | None = None) -> list:
    """The k most similar accepted records for `job`, best first, each at or
    above its method's min_score: [(score, record)]."""
    k = _L["shots"]["max"] if k is None else k
    pool = [r for r in records if r.get("job") == job and r.get("accepted")]
    if not pool or k <= 0 or not (query or "").strip():
        return []
    qvec = _embed(query, embed) if any(r.get("vec") for r in pool) or embed is not None else None
    scored = []
    for n, rec in enumerate(pool):
        if qvec is not None and rec.get("vec"):
            score, floor = _cosine(qvec, rec["vec"]), _L["embed"]["min_score"]
        else:
            score, floor = _overlap(query, rec.get("input", "")), _L["shots"]["min_score"]
        if score >= floor:
            scored.append((score, n, rec))
    scored.sort(key=lambda s: (-s[0], -s[1]))  # ties: the newer first
    return [(score, rec) for score, _n, rec in scored[:k]]


def block(records: list, job: str, query: str, embed=None) -> str:
    """The few-shot prompt block for a local call: the most similar accepted
    examples, each whole, within shots.budget_chars. '' when there are none."""
    s = _L["shots"]
    if not (_L["enabled"] and s["enabled"] and job in s["jobs"]):
        return ""
    picked, used = [], 0
    for _score, rec in similar(records, job, query, embed):
        shot = s["shot"].format(input=rec["input"], why=rec.get("why", ""), local=rec.get("local", ""),
                                accepted=rec["accepted"])
        if used + len(shot) > s["budget_chars"]:
            continue
        picked.append(shot)
        used += len(shot)
    return s["intro"] + "".join(picked) if picked else ""


def shots(paths: Paths | None, job: str, query: str, embed=None) -> str:
    """block() over the store, for a local call. No paths: none."""
    if paths is None:
        return ""
    try:
        return block(load(paths), job, query, embed)
    except Exception as exc:  # noqa: BLE001 -- a broken store never stops the local call
        log.info(_L["say_store_failed"].format(why=exc))
        return ""


# -- the weekly report -------------------------------------------------------------

def report(paths: Paths, days: int | None = None, today: dt.date | None = None, records: list | None = None) -> dict:
    """{since, jobs: {job: {model: {total, accepted, reasons: [(why, n)]}}}} for
    the records of the last `days` days (learn.report.days)."""
    r = _L["report"]
    days = r["days"] if days is None else days
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=days)).isoformat()
    records = load(paths) if records is None else records
    jobs: dict = {}
    for rec in records:
        if str(rec.get("when", ""))[:10] < since:
            continue
        slot = jobs.setdefault(rec.get("job", ""), {}).setdefault(rec.get("model") or r["no_model"],
                                                                  {"total": 0, "accepted": 0,
                                                                   "reasons": collections.Counter()})
        slot["total"] += 1
        slot["accepted"] += 1 if rec.get("accepted") else 0
        slot["reasons"][rec.get("why") or r["no_reason"]] += 1
    for by_model in jobs.values():
        for slot in by_model.values():
            slot["reasons"] = slot["reasons"].most_common(r["top"])
    return {"since": since, "until": today.isoformat(), "days": days, "jobs": jobs}


def reason_label(job: str, why: str) -> str:
    return _L["reasons"].get(job, {}).get(why, why)


def render_report(rep: dict) -> str:
    r = _L["report"]
    lines = [r["heading"].format(**rep)]
    if not rep["jobs"]:
        lines.append(r["none"])
    for job in sorted(rep["jobs"]):
        for model_name, slot in sorted(rep["jobs"][job].items()):
            lines.append(r["job_line"].format(job=job, model=model_name, **{k: slot[k] for k in ("total", "accepted")}))
            lines += [r["reason_line"].format(n=n, why=reason_label(job, why)) for why, n in slot["reasons"]]
    return "\n".join(lines)
