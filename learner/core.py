"""Chuen's learner model, after Thomas van Pul's (Atrium, Oct 2026).

A small store of what is known about how Chuen learns and works, read by every
agent before it plans anything about him (study plans, order of work, what to
ask him). Rules, from Thomas's AI-RULES:
  * every belief says where it came from: stated (Chuen said it), answered (his
    reply to a bank question), or measured (from data, with the evidence named).
    Nothing is inferred by a model and stored as fact.
  * questions about him go into a bank and are asked a few at a time, never
    re-asked once answered; an "unsure" answer is marked to be measured instead.
  * the note agents read (Learner-Model.md) is generated from the store, never
    hand-edited.

Store: <vault>/Dev/learner/model.json (the vault is private; this repo is public).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

VAULT = Path(os.environ.get("DENISSE_VAULT",
                            Path.home() / "OneDrive - King's College London" / "Denisse"))
STORE = Path(os.environ.get("LEARNER_STORE", VAULT / "Dev" / "learner" / "model.json"))
NOTE = VAULT / "Atlas" / "Concepts" / "Learning" / "Learner-Model.md"
SOURCES = ("stated", "answered", "measured")
AREAS = ("study", "format", "schedule", "attention", "struggles", "goals", "feedback")
ASK_AGAIN_DAYS = 7

# Starting question bank. Worded to be answerable in one line; each names the
# area it fills. Asked a few at a time (see next_questions), never all at once.
SEED_QUESTIONS = [
    ("format", "When you learn a new topic (say a new Signals and Systems lecture), what works best first: a worked example, the theory and derivation, or a picture or diagram?"),
    ("format", "Do you prefer notes as short bullet points, full sentences, or mostly equations with a line of explanation each?"),
    ("study", "How long can you study in one go before you need a break, roughly?"),
    ("schedule", "What time of day do you do your best focused work?"),
    ("schedule", "Which days or times are usually taken (lectures, society, work) and shouldn't get study blocks?"),
    ("feedback", "When an AI quizzes you, do you want the answer straight after each question, or all answers at the end?"),
    ("feedback", "Should reminders be gentle, or blunt when you're behind?"),
    ("struggles", "Which module or topic this term feels hardest right now?"),
    ("struggles", "When you get stuck on a problem, what usually helps: a hint, the next step, or the full solution to compare against?"),
    ("attention", "What most often pulls you off task when you're studying?"),
    ("goals", "What result are you aiming for this year (overall, or per module)?"),
    ("goals", "Besides grades, what are you building towards this year (internship, projects, society)?"),
]


def now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def load() -> dict:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("beliefs", [])
    data.setdefault("questions", [])
    return data


def save(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STORE)


def _next_id(items: list, prefix: str) -> str:
    n = max([int(i["id"][1:]) for i in items if re.fullmatch(prefix + r"\d+", i["id"])] or [0]) + 1
    return f"{prefix}{n}"


def seed(data: dict) -> int:
    """Add any seed question not already in the bank (by text). Idempotent."""
    have = {q["text"] for q in data["questions"]}
    added = 0
    for area, text in SEED_QUESTIONS:
        if text not in have:
            data["questions"].append({"id": _next_id(data["questions"], "q"), "area": area, "text": text,
                                      "status": "open", "asked_at": None, "asked_count": 0})
            added += 1
    return added


def add_belief(data: dict, claim: str, area: str, source: str, evidence: str = "", question: str | None = None) -> dict:
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    if area not in AREAS:
        raise ValueError(f"area must be one of {AREAS}")
    if source == "measured" and not evidence:
        raise ValueError("a measured belief must name its evidence")
    claim = claim.strip()
    for b in data["beliefs"]:
        if b["claim"].lower() == claim.lower() and b["status"] == "current":
            return b  # already known
    b = {"id": _next_id(data["beliefs"], "b"), "claim": claim, "area": area, "source": source,
         "evidence": evidence, "question": question, "date": now(), "status": "current"}
    data["beliefs"].append(b)
    return b


def supersede(data: dict, belief_id: str, new_claim: str, source: str = "stated", evidence: str = "") -> dict:
    old = next(b for b in data["beliefs"] if b["id"] == belief_id)
    old["status"] = "superseded"
    new = add_belief(data, new_claim, old["area"], source, evidence)
    old["superseded_by"] = new["id"]
    return new


def next_questions(data: dict, n: int = 2) -> list[dict]:
    """Up to n open questions, areas with no beliefs first, never one asked in the
    last ASK_AGAIN_DAYS days. Marks them asked."""
    known = {b["area"] for b in data["beliefs"] if b["status"] == "current"}
    cutoff = dt.datetime.now() - dt.timedelta(days=ASK_AGAIN_DAYS)
    ready = [q for q in data["questions"] if q["status"] == "open"
             and (q["asked_at"] is None or dt.datetime.fromisoformat(q["asked_at"]) < cutoff)]
    ready.sort(key=lambda q: (q["area"] in known, q["asked_count"], q["id"]))
    picked, areas = [], set()
    for q in ready:  # one per area per round, so a round covers different ground
        if q["area"] not in areas:
            picked.append(q)
            areas.add(q["area"])
        if len(picked) == n:
            break
    for q in picked:
        q["asked_at"] = now()
        q["asked_count"] += 1
    return picked


def answer(data: dict, qid: str, text: str, unsure: bool = False) -> dict | None:
    q = next(x for x in data["questions"] if x["id"] == qid)
    if unsure:
        q["status"] = "measure"  # not re-asked the same way; find evidence instead
        q["answered_at"] = now()
        return None
    q["status"] = "answered"
    q["answered_at"] = now()
    return add_belief(data, text, q["area"], "answered", question=q["text"])


def render(data: dict) -> str:
    today = dt.date.today().isoformat()
    cur = [b for b in data["beliefs"] if b["status"] == "current"]
    lines = [
        "---", "name: Learner-Model",
        "description: Generated model of how Chuen learns and works. Read the first section before planning anything about him.",
        f"updated: {today}", "type: reference", "status: active", "tags: [learning, generated]", "protection: locked", "---", "",
        "# Learner model", "",
        "_Generated by `python -m learner render` from `Dev/learner/model.json`. Don't edit by hand: "
        "add facts with `python -m learner add` or answer the bank with `python -m learner answer`._", "",
        "## Before planning anything about Chuen", "",
        "Use only what is below. Anything not listed is **unknown**: don't assume it, and don't ask him in chat. "
        "Questions go to the bank (`python -m learner ask`), a couple at a time.", "",
    ]
    for area in AREAS:
        items = [b for b in cur if b["area"] == area]
        lines.append(f"### {area.title()}")
        if items:
            for b in items:
                src = b["source"] + (f": {b['evidence']}" if b.get("evidence") else "")
                lines.append(f"- {b['claim']} _({src}, {b['date'][:10]})_")
        else:
            lines.append("- unknown")
        lines.append("")
    measure = [q for q in data["questions"] if q["status"] == "measure"]
    if measure:
        lines += ["### To measure, not ask again"] + [f"- {q['text']}" for q in measure] + [""]
    open_n = sum(1 for q in data["questions"] if q["status"] == "open")
    lines += ["## Bank", "", f"- {open_n} open question(s), {len(cur)} current belief(s).",
              "- How Chuen wants material that teaches him to be written: [[How-Chuen-Learns]].", ""]
    return "\n".join(lines)


def write_note(data: dict) -> Path:
    NOTE.parent.mkdir(parents=True, exist_ok=True)
    NOTE.write_text(render(data), encoding="utf-8")
    return NOTE
