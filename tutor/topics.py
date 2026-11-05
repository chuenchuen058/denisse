"""The per-module topic map: memory/tutor/topics-<slug>.json.

    {"module": ..., "code": ..., "updated": ...,
     "topics": [{"name": ..., "lectures": [weeks], "prerequisites": [...],
                 "coursework": ["Q2 ..."]}]}

Topics come from what the model listed for each week (state.lectures);
a topic named in two weeks is one topic covering both. Coursework links
come from the module's Coursework note read by study_brief.read_coursework
-- the same reader the study brief uses -- by word overlap between a
question's topic and the topic's name. Later tasks (2-4) read this map to
pick what to teach, what to ask and where he is.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re

from . import conf
from .conf import CONFIG, Module, Paths

_T = CONFIG["topics"]
_STOP = set(_T["stop_words"])


def _words(text: str) -> set:
    return {w for w in conf.words(text) if len(w) >= _T["match_min_word_chars"] and w not in _STOP}


def coursework_items(module: Module) -> list:
    """[{label, words}] for every coursework question or item with a topic.
    No coursework note, or study_brief can't read it: none."""
    out = []
    for pattern in CONFIG["paths"]["coursework_globs"]:
        for path in sorted(glob.glob(os.path.join(glob.escape(module.folder), pattern))):
            try:
                import study_brief
                cw = study_brief.read_coursework(path)
            except Exception:  # noqa: BLE001 -- the map is still worth having without links
                continue
            for item in cw["items"]:
                topic = item.get("topic") or item.get("text", "")
                label = item["text"] if not item.get("question") else re.sub(r"\s+", " ", item["text"]).strip()
                if _words(topic):
                    out.append({"label": label, "words": _words(topic)})
    return out


def build(module: Module, lectures: dict, today: dt.date | None = None) -> dict:
    """The map from state.lectures[slug] (week -> {"topics": [...]})."""
    today = today or dt.date.today()
    merged = {}
    for week in sorted(lectures, key=int):
        for t in lectures[week].get("topics", []):
            key = t["name"].lower()
            rec = merged.setdefault(key, {"name": t["name"], "lectures": [], "prerequisites": [], "coursework": []})
            if int(week) not in rec["lectures"]:
                rec["lectures"].append(int(week))
            for p in t.get("prerequisites", []):
                if p.lower() not in {x.lower() for x in rec["prerequisites"]}:
                    rec["prerequisites"].append(p)
    items = coursework_items(module)
    for rec in merged.values():
        # Every word of the topic's name must be in the question's topic:
        # 'transform' alone must not tie Fourier to a Laplace question.
        tw = _words(rec["name"])
        rec["coursework"] = [i["label"] for i in items if tw and tw <= i["words"]]
    return {"module": module.title, "code": module.code, "updated": today.isoformat(),
            "topics": sorted(merged.values(), key=lambda r: (r["lectures"][0], r["name"].lower()))}


def save(paths: Paths, module: Module, topic_map: dict) -> str:
    path = paths.topics_file(module.slug)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(topic_map, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load(paths: Paths, module: Module) -> dict | None:
    try:
        with open(paths.topics_file(module.slug), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
