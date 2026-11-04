"""
Denisse -- learn from how real conversations actually went.

The nightly eval (eval/run_eval.py) already harvests verified-good answers
into eval/dataset.jsonl, but it only ever sees the fixed prompts in
eval/cases.py and skips any prompt it has stored before. By 2026-10-19 it
had sat at 12 lines since 2026-09-04: the set it learns from had run dry,
while the conversations Chuen actually has never reached it at all.

Those conversations carry the signal. From memory/conversation.json on
2026-10-19:

    "study time"    -> call_skill('study')     -> "No skill named 'study'"
                    -> "You can use the Pomodoro Timer skill by following..."
    "turn it off"   -> spotify x2, both "Spotify doesn't see an active device"
                    -> "Spotify is paused."
    "i meant the timer turn it off i finished studying"

Nobody marked those as failures, but the turn says so itself: every tool
it called failed and the reply claimed success anyway, and the next thing
he typed was "i meant ...". So this module reads the stored history and
labels each turn from that implicit feedback:

  bad      the next user message corrects it ("no", "i meant", "wrong"),
           or its tools failed and the reply doesn't admit it
  good     he thanked her for it
  unknown  everything else -- most chat, and no guess is better than a
           wrong one in a set meant for training

"Good" is strict on purpose. A first version also counted turns whose
tools succeeded and that he didn't correct; on the real history that
labelled a new "remind me to reply to ..." -> "Your task list is
still there" as good. He just moved on. Silence isn't approval.

Bad turns become candidate eval cases (the roadmap's "every bug found
becomes a case"), grouped so a repeated failure shows up once with a
count. Good turns become candidate training pairs. Both go to
memory/learned.json, which is gitignored: this is his real conversation,
and it must never land in the public repo.

Nothing here is fed back into the prompt. Injecting context into an 8B
model has measurably hurt before (decisions.md, 2026-09-02), so using what
is learned at runtime is a separate change that needs its own A/B.

Run:
    python learning.py
"""

import datetime
import json
import os
import re
from pathlib import Path

import json_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# What counts as a tool failing, a reply admitting it, a reply claiming an
# action, and his correction or praise -- every phrase list -- plus the
# report's sizes live in learning.json (AGENTS.md Rule 0), checked by
# json_config.py against learning.defaults.json; configure() reloads it.
# The grammar stays here: a correction is any number of lead-ins then a
# phrase at the start; praise is at most one lead-in then a phrase.
CONFIG_ENV_VAR = "DENISSE_LEARNING_CONFIG"
CONFIG_FILE = Path(__file__).with_name("learning.json")
DEFAULTS_FILE = Path(__file__).with_name("learning.defaults.json")

_NEVER = re.compile(r"(?!)")


def _alt(fragments) -> str:
    return "|".join(fragments)


def _compile(pattern: str):
    return re.compile(pattern, re.IGNORECASE) if pattern else _NEVER


def _tool_failure(c):
    parts = ([rf"^(?:{_alt(c['starts_with'])})"] if c["starts_with"] else []) + list(c["contains"])
    return _compile(_alt(parts))


def _admits_failure(c):
    return _compile(rf"\b(?:{_alt(c['words'])})\b" if c["words"] else "")


def _lead_in(c, repeat: str):
    lead = rf"(?:(?:{_alt(c['lead_ins'])})[,!.]?\s+){repeat}" if c["lead_ins"] else ""
    return _compile(rf"^\s*{lead}(?:{_alt(c['phrases'])})" if c["phrases"] else "")


_BUILDERS = {
    "tool_failure": (("starts_with", "contains"), _tool_failure),
    "admits_failure": (("words",), _admits_failure),
    "correction": (("lead_ins", "phrases"), lambda c: _lead_in(c, "*")),
    "praise": (("lead_ins", "phrases"), lambda c: _lead_in(c, "?")),
    "claims_done": (("patterns",), lambda c: _compile(_alt(c["patterns"]))),
}


def _extra_checks(data: dict, defaults: dict, checker) -> dict:
    """Every fragment must compile, alone and once built into its pattern."""
    for section, (keys, build) in _BUILDERS.items():
        try:
            for key in keys:
                for frag in data[section][key]:
                    if not frag:
                        raise re.error(f"empty pattern in {key}")
                    re.compile(frag)
            build(data[section])
        except re.error as e:
            data[section] = checker.bad(f"learning.{section}", f"has a bad pattern ({e})", defaults[section])
    return data


CONFIG: dict = {}


def configure(path=None) -> dict:
    """(Re)load learning.json (or ``path``, or $DENISSE_LEARNING_CONFIG)
    and rebind the module patterns from it.

    brain.py binds CLAIMS_DONE once at import; a reload reaches it on the
    next start.
    """
    global CONFIG, LEARNED_FILE, _TOOL_FAILURE, _ADMITS_FAILURE, _CORRECTION, CLAIMS_DONE, _THANKS
    path = path or os.environ.get(CONFIG_ENV_VAR) or CONFIG_FILE
    CONFIG = json_config.load("learning", path, DEFAULTS_FILE, extra=_extra_checks)
    LEARNED_FILE = os.path.join(BASE_DIR, *CONFIG["learned_file"].replace("\\", "/").split("/"))
    # Tool results that mean the call did not do what was asked.
    _TOOL_FAILURE = _BUILDERS["tool_failure"][1](CONFIG["tool_failure"])
    # A reply that owns up to a failure.
    _ADMITS_FAILURE = _BUILDERS["admits_failure"][1](CONFIG["admits_failure"])
    # The next user message telling her the last reply was wrong.
    _CORRECTION = _BUILDERS["correction"][1](CONFIG["correction"])
    # A reply saying something was DONE (shared with brain.py).
    CLAIMS_DONE = _BUILDERS["claims_done"][1](CONFIG["claims_done"])
    # Approval of the reply. 2026-10-30: his real praise was "thats good"
    # and "okay that good", and neither counted -- 0 good turns in 146, so
    # no lesson could ever be confirmed.
    _THANKS = _BUILDERS["praise"][1](CONFIG["praise"])
    return CONFIG


configure()


def tool_failed(result: str) -> bool:
    return bool(_TOOL_FAILURE.search((result or "").strip()))


def split_turns(history: list) -> list:
    """Group the flat stored history into turns: one user message and
    everything Denisse did before the next one."""
    turns = []
    for m in history:
        role = m.get("role")
        if role == "user":
            turns.append({"user": m.get("content") or "", "tools": [], "results": [], "reply": "",
                          "id": m.get("turn_id")})
        elif not turns:
            continue
        elif role == "assistant":
            for call in m.get("tool_calls") or []:
                name = (call.get("function") or {}).get("name")
                if name:
                    turns[-1]["tools"].append(name)
            if (m.get("content") or "").strip():
                turns[-1]["reply"] = m["content"].strip()
        elif role == "tool":
            turns[-1]["results"].append(m.get("content") or "")
    return turns


def label(turn: dict, next_user: str | None) -> tuple[str, str]:
    """(label, reason) for one turn, judged by its tool results and by
    what Chuen said next."""
    if next_user and _CORRECTION.search(next_user):
        return "bad", "corrected"

    failed = [r for r in turn["results"] if tool_failed(r)]
    if turn["results"] and len(failed) == len(turn["results"]):
        if not turn["reply"] or not _ADMITS_FAILURE.search(turn["reply"]):
            return "bad", "claimed success after tools failed"

    if not turn["tools"] and CLAIMS_DONE.search(turn["reply"] or ""):
        return "bad", "claimed an action with no tool call"

    if next_user and _THANKS.search(next_user) and turn["reply"]:
        return "good", "thanked"
    return "unknown", ""


def label_turns(history: list) -> list:
    """Every turn in order, each with its label -- instincts.py reads the
    sequence (a failure, then what worked), not just the failures."""
    turns = split_turns(history)
    labelled = []
    for i, turn in enumerate(turns):
        next_user = turns[i + 1]["user"] if i + 1 < len(turns) else None
        verdict, reason = label(turn, next_user)
        labelled.append({**turn, "label": verdict, "reason": reason, "next_user": next_user})
    # Praise taken back on the very next message is not approval. Live on
    # 29 Oct: a clock-only "briefing" got "thats good", then "what?" -- and
    # counted as good, it proposed routing "alright tell me" to the clock.
    for i, t in enumerate(labelled[:-1]):
        if t["label"] == "good" and labelled[i + 1]["label"] == "bad" \
                and labelled[i + 1]["reason"] == "corrected":
            t["label"], t["reason"] = "unknown", "praise withdrawn"
    return labelled


def learn(history: list) -> dict:
    """Label every turn and collect what's worth keeping."""
    labelled = label_turns(history)

    # One entry per distinct prompt: the same failure every day is one bug
    # with a count, not a page of duplicates.
    failures = {}
    for t in labelled:
        if t["label"] != "bad":
            continue
        key = " ".join(t["user"].lower().split())
        entry = failures.setdefault(key, {
            "prompt": t["user"], "reason": t["reason"], "tools": t["tools"],
            "reply": t["reply"], "next_user": t["next_user"], "count": 0,
        })
        entry["count"] += 1

    good, seen = [], set()
    for t in labelled:
        key = " ".join(t["user"].lower().split())
        if t["label"] != "good" or key in seen:
            continue
        seen.add(key)
        good.append({
            "messages": [
                {"role": "user", "content": t["user"]},
                {"role": "assistant", "content": t["reply"]},
            ],
            "tools": t["tools"],
            "reason": t["reason"],
        })

    counts = {"good": 0, "bad": 0, "unknown": 0}
    for t in labelled:
        counts[t["label"]] += 1

    return {
        "turns": len(labelled),
        "counts": counts,
        "failures": sorted(failures.values(), key=lambda e: -e["count"]),
        "good": good,
    }


def run(history: list | None = None, out_file: str | None = None) -> dict:
    """Learn from the stored history and write memory/learned.json."""
    out_file = out_file or LEARNED_FILE
    if history is None:
        import memory_store
        history = memory_store.load_history()
    result = learn(history)
    result["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return result


def summary(result: dict, limit: int | None = None) -> list:
    """Markdown lines for the nightly eval report."""
    limit = CONFIG["report"]["limit"] if limit is None else limit
    c = result["counts"]
    lines = [
        f"- {result['turns']} live turns: {c['good']} good, {c['bad']} bad, "
        f"{c['unknown']} unlabelled",
        f"- {len(result['failures'])} distinct failing prompts, "
        f"{len(result['good'])} candidate training pairs",
    ]
    if result["failures"]:
        lines += ["", "Candidate eval cases (worst first):", ""]
        for f in result["failures"][:limit]:
            times = f" ×{f['count']}" if f["count"] > 1 else ""
            lines.append(f"- `{f['prompt'][:CONFIG['report']['prompt_chars']]}`{times} — {f['reason']}; "
                         f"tools `{f['tools'] or 'none'}`")
    return lines


def main() -> None:
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    result = run()
    print("\n".join(summary(result, limit=CONFIG["report"]["cli_limit"])))
    print(LEARNED_FILE)


if __name__ == "__main__":
    main()
