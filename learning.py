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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEARNED_FILE = os.path.join(BASE_DIR, "memory", "learned.json")

# Tool results that mean the call did not do what was asked. Matched at the
# start of the result or as a whole phrase -- tools report failure in prose,
# and a substring like "error" alone would also match a news headline.
_TOOL_FAILURE = re.compile(
    r"^(error\b|failed\b|could ?n[o']t\b|unable to\b|no skill named\b|unknown tool\b)"
    r"|doesn't see an active device"
    r"|\bnot found\b"
    r"|\bno such file\b"
    r"|\bpermission denied\b"
    r"|^the system cannot find\b"
    r"|is not recognized as an internal or external command"
    r"|^no files matching\b"
    r"|^no single open task matched\b"
    r"|^don'?t know a location\b",
    re.IGNORECASE,
)

# A reply that owns up to a failure. If the tools failed and the reply says
# one of these, she handled it honestly and the turn isn't held against her.
_ADMITS_FAILURE = re.compile(
    r"\b(couldn'?t|could not|can'?t|cannot|unable|sorry|didn'?t work|doesn'?t|"
    r"isn'?t|wasn'?t|failed|no skill|not (?:able|available|found|open|running))\b",
    re.IGNORECASE,
)

# The next user message telling her the last reply was wrong. Anchored at
# the start: "no" inside a sentence ("no rush") is not a correction.
_CORRECTION = re.compile(
    r"^\s*(no\b(?!\s+(?:worries|problem|rush|thanks))|nope\b|wrong\b|not that\b|that'?s (?:not|wrong)\b|i meant\b|"
    r"i said\b|not what i\b|you didn'?t\b|that didn'?t\b|it didn'?t\b)",
    re.IGNORECASE,
)

# A reply saying something was DONE. brain.py uses the same pattern live,
# to send her back before a false "done" reaches him; here it labels the
# ones that got through. From 2026-10-20: "stop timer" -> no tool -> "Done."
CLAIMS_DONE = re.compile(
    r"^\s*(?:done|all done|all set|sorted|ok(?:ay)?[,.]?\s+done)\b\s*(?:[.!,:]|$)"
    r"|\b(?:has|have) been (?:started|stopped|set|turned (?:on|off)|completed|opened"
    r"|closed|cancell?ed|created|saved|added|removed|paused|played|scheduled)\b"
    r"|\bi(?:'ve| have)?\s+(?:just\s+)?(?:started|stopped|set|turned|opened|closed"
    r"|cancell?ed|created|saved|added|removed|paused|scheduled)\b"
    r"|\b(?:has|is now)\s+(?:started|stopped|running)\b"
    r"|\b(?:timer|pomodoro|spotify|music)\s+is\s+(?:running|paused|playing|off|on)\b"
    r"|\b(?:file|document|cv|resume)\b.{0,40}\bis\s+(?:now\s+)?open\b"
    r"|\bsuccessfully\b",
    re.IGNORECASE,
)

_THANKS = re.compile(
    r"^\s*(thanks|thank you|thx|cheers|perfect|great|nice|awesome|good job|"
    r"that works|that'?s right)\b",
    re.IGNORECASE,
)


def tool_failed(result: str) -> bool:
    return bool(_TOOL_FAILURE.search((result or "").strip()))


def split_turns(history: list) -> list:
    """Group the flat stored history into turns: one user message and
    everything Denisse did before the next one."""
    turns = []
    for m in history:
        role = m.get("role")
        if role == "user":
            turns.append({"user": m.get("content") or "", "tools": [], "results": [], "reply": ""})
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


def run(history: list | None = None, out_file: str = LEARNED_FILE) -> dict:
    """Learn from the stored history and write memory/learned.json."""
    if history is None:
        import memory_store
        history = memory_store.load_history()
    result = learn(history)
    result["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return result


def summary(result: dict, limit: int = 5) -> list:
    """Markdown lines for the nightly eval report."""
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
            lines.append(f"- `{f['prompt'][:80]}`{times} — {f['reason']}; "
                         f"tools `{f['tools'] or 'none'}`")
    return lines


def main() -> None:
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    result = run()
    print("\n".join(summary(result, limit=20)))
    print(LEARNED_FILE)


if __name__ == "__main__":
    main()
