"""Turn what learning.py found into draft regression cases.

The loop today stops one step short. learning.py labels live turns and writes
memory/learned.json; run_eval.py prints them under "Learned from live use".
Then a human reads the report and fixes the bug -- which is exactly what
happened to "study time" on 2026-10-19 (failed at 15:01, fixed at 18:06).
Nothing stops it coming back, because nothing turns the failure into a case.
The roadmap already asks for this: "Every bug found becomes a case."

So this proposes cases. It deliberately does NOT write eval/cases.py:

  * The prompt and the evidence are mechanical -- this script can derive them.
  * The assertion is a judgement. For "study time" the right expect is "calls
    the study-mode tool", and no script can know that from a failed turn.

A wrong eval case is worse than no eval case: it goes red forever and gets
ignored, or goes green while the bug lives. So each draft carries its
evidence and a REVIEW marker, and a human (or a Claude session reading this
file) decides the assertion and moves it across.

    python scripts/propose_cases.py            # print drafts
    python scripts/propose_cases.py --write    # also write eval/proposed_cases.py
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEARNED = os.path.join(ROOT, "memory", "learned.json")
CASES = os.path.join(ROOT, "eval", "cases.py")
OUT = os.path.join(ROOT, "eval", "proposed_cases.py")
WRITE = "--write" in sys.argv


def norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def existing_prompts() -> set:
    """Prompts already covered, so a fixed bug isn't proposed twice."""
    with open(CASES, encoding="utf-8") as f:
        src = f.read()
    return {norm(m) for m in re.findall(r'"prompt":\s*"((?:[^"\\]|\\.)*)"', src)}


def handled_before_the_model(prompt: str) -> bool:
    """Is this prompt intercepted before it ever reaches the model?

    study_mode.match_command catches the study/pomodoro phrases in tray.py
    and wake_word.py, with no model call -- so an eval case for one would
    measure the wrong thing, and `scripts/test_study_command.py` is where it
    is really tested. Without this check the five study-mode failures of
    2026-10-19 would be re-proposed every night forever: they are fixed, but
    fixed somewhere eval/cases.py cannot see.

    Checked with active=True as well, since a phrase like "turn it off" is a
    stop command only while the timer runs -- which is when it was said.
    """
    try:
        sys.path.insert(0, ROOT)
        import study_mode
    except Exception:
        return False  # can't tell (no Tk, no PIL) -- propose it and let a human look
    return bool(study_mode.match_command(prompt, active=False)
                or study_mode.match_command(prompt, active=True))


def slug(text: str, used: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", norm(text))[:28].strip("_") or "case"
    name, n = base, 2
    while name in used:
        name, n = f"{base}_{n}", n + 1
    used.add(name)
    return name


def suggest(failure: dict) -> tuple[str, str]:
    """(suggested expect, what the reviewer has to decide).

    Only the 'claimed success after tools failed' shape yields an assertion
    worth writing automatically, and even then it asserts HONESTY, not
    correctness -- that she must not claim success when every tool failed.
    Asserting the right tool would need to know what the right tool was.
    """
    reason = failure.get("reason", "")
    tools = failure.get("tools") or []
    reply = (failure.get("reply") or "").strip()

    if reason == "claimed success after tools failed":
        claim = " ".join(reply.split())[:60]
        expect = (
            '{"deny": [%r],\n'
            '                   "must_contain": []}  # REVIEW: either keep deny,\n'
            "                   # or replace with any_tool_of={the tool that should\n"
            "                   # have been called} once you know what that is"
            % (claim,)
        )
        decide = ("she claimed success while every tool failed. The deny above "
                  "pins the exact false claim; a tool assertion would be stronger.")
        return expect, decide

    if reason == "corrected":
        nxt = " ".join((failure.get("next_user") or "").split())[:70]
        expect = (
            '{}  # REVIEW: no assertion can be derived. He corrected her with\n'
            "                   # %r\n"
            "                   # Decide what the right behaviour was and assert that."
            % (nxt,)
        )
        decide = f"he corrected her with {nxt!r} — the right behaviour is a judgement call."
        return expect, decide

    return '{}  # REVIEW: unclassified', "unknown failure shape."


def main() -> None:
    if not os.path.exists(LEARNED):
        print("  no memory/learned.json yet — run learning.py first")
        return
    with open(LEARNED, encoding="utf-8") as f:
        learned = json.load(f)

    failures = learned.get("failures", [])
    covered = existing_prompts()
    print(f"learned.json: {learned.get('turns')} turns, "
          f"{learned['counts']['bad']} bad, {len(failures)} distinct failing prompts")
    print(f"eval/cases.py already covers {len(covered)} prompts\n")

    used, drafts = set(), []
    skipped = intercepted = 0
    for fo in failures:
        if norm(fo["prompt"]) in covered:
            skipped += 1
            continue
        if handled_before_the_model(fo["prompt"]):
            intercepted += 1
            continue
        expect, decide = suggest(fo)
        name = slug(fo["prompt"], used)
        times = f" (seen {fo['count']}x)" if fo.get("count", 1) > 1 else ""
        drafts.append({
            "id": name, "prompt": fo["prompt"], "expect": expect,
            "why": f"Live failure{times}: {fo.get('reason')}. "
                   f"Tools called: {fo.get('tools') or 'none'}.",
            "decide": decide,
            "evidence_reply": (fo.get("reply") or "")[:200],
            "evidence_next": (fo.get("next_user") or "")[:120],
        })

    if skipped:
        print(f"  {skipped} already covered by a case — not proposed again")
    if intercepted:
        print(f"  {intercepted} now handled before the model (study_mode.match_command) —"
              f" tested in scripts/test_study_command.py, not here")
    if skipped or intercepted:
        print()
    if not drafts:
        print("  nothing new to propose. The loop is caught up.")
        return

    print(f"  {len(drafts)} draft case(s) need a human decision:\n")
    for d in drafts:
        print(f"  --- {d['id']}")
        print(f"      prompt   : {d['prompt'][:90]}")
        print(f"      she said : {d['evidence_reply'][:90]}")
        if d["evidence_next"]:
            print(f"      he replied: {d['evidence_next'][:90]}")
        print(f"      DECIDE   : {d['decide']}")
        print()

    if WRITE:
        with open(OUT, "w", encoding="utf-8", newline="\n") as f:
            f.write('"""Draft regression cases proposed from live failures.\n\n'
                    "NOT imported by the runner. Each needs its assertion decided, then\n"
                    "moved into eval/cases.py by hand. Regenerate with:\n"
                    "    python scripts/propose_cases.py --write\n"
                    '"""\n\nPROPOSED = [\n')
            for d in drafts:
                f.write("    {\n")
                f.write(f'        "id": {d["id"]!r},\n')
                f.write(f'        "prompt": {d["prompt"]!r},\n')
                f.write(f'        "expect": {d["expect"]}\n')
                f.write(f'        ,\n        "why": {d["why"]!r},\n')
                f.write("    },\n")
            f.write("]\n")
        print(f"  wrote {OUT}")
    else:
        print("  re-run with --write to save eval/proposed_cases.py")


if __name__ == "__main__":
    main()
