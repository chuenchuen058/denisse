"""Real local-model eval: reminder and timer phrases, end to end, on a fixed clock.

Each case sends one "remind me ..." through the real brain (Ollama) with
time_parse's clock pinned to Wednesday 11 Nov 2026 10:00 London -- not
today, so anything still reading the real clock shows up as a failure. A
case passes when the right tool ran, the stored deadline (or timer length)
is the expected one, and the reply echoes the resolved time in words.
Tasks go to run_eval's scratch store; timers are in-process with no
notifier and are cancelled after each case. Never touches his real tasks.

    .venv\\Scripts\\python.exe scripts\\eval_time_parse.py --rounds 3
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CLOCK = datetime(2026, 11, 11, 10, 0)  # a Wednesday

CASES = [
    {"id": "weekday_bare_hour", "prompt": "remind me to call the dentist thursday at 3",
     "due": "2026-11-12T15:00", "echo": "Thursday 12 Nov, 15:00"},
    {"id": "tomorrow_morning", "prompt": "remind me to submit the lab report tomorrow morning",
     "due": "2026-11-12T09:00", "echo": "Thursday 12 Nov, 09:00"},
    {"id": "next_monday_lecture", "prompt": "remind me to email Sunway next Monday before the lecture",
     "due": "2026-11-16T23:59", "echo": "Monday 16 Nov, 23:59"},
    {"id": "half_past", "prompt": "remind me to ring mum at half past four",
     "due": "2026-11-11T16:30", "echo": "Wednesday 11 Nov, 16:30"},
    {"id": "ordinal", "prompt": "remind me to pay the rent on the 12th",
     "due": "2026-11-12T23:59", "echo": "Thursday 12 Nov, 23:59"},
    {"id": "tomorrow_evening", "prompt": "remind me to collect my prescription tomorrow evening",
     "due": "2026-11-12T18:00", "echo": "Thursday 12 Nov, 18:00"},
    # Today is Wednesday and 15:00 hasn't come: today or next week? She must ask.
    {"id": "ambiguous_today", "prompt": "remind me to water the plants wednesday at 3",
     "ask": True, "word": "plants"},
    # A span from now: a timer of 20 minutes, or a task due 10:20 -- either is right.
    {"id": "in_20_mins", "prompt": "remind me in 20 mins to check the oven",
     "minutes": 20, "due": "2026-11-11T10:20"},
]


def grade(case, result, store, running):
    """Failures for one case: [] is a pass."""
    fails = list(result["failures"])
    reply = result["reply"]
    if case.get("ask"):
        if any(case["word"] in t["text"] for t in store):
            fails.append("added a task for an ambiguous day instead of asking")
        if "?" not in reply:
            fails.append("didn't ask which day")
        return fails
    if "minutes" in case:
        timer_ok = any(abs(t["minutes"] - case["minutes"]) < 0.01 for t in running)
        task_ok = any(t["due"] == case["due"] for t in store)
        if not (timer_ok or task_ok):
            fails.append(f"no {case['minutes']}-minute timer and no task due {case['due']} "
                         f"(timers={[t['minutes'] for t in running]}, tasks={[t['due'] for t in store]})")
        return fails
    dues = [t["due"] for t in store if t["text"] != "file the Spritzer paperwork"]
    if case["due"] not in dues:
        fails.append(f"stored due {dues}, want {case['due']}")
    if case["echo"] not in reply:
        fails.append(f"reply doesn't echo {case['echo']!r}")
    return fails


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--out", type=Path, help="also write every record here as JSON")
    ap.add_argument("--wait-gpu", type=int, default=300, metavar="SECONDS")
    args = ap.parse_args()
    if not 1 <= args.rounds <= 5:
        ap.error("rounds must be between 1 and 5")

    sys.path.insert(0, str(ROOT / "eval"))
    from eval import run_eval
    import conversation
    import tasks
    import time_parse
    import timers

    lease = run_eval._gpu_lease(wait_s=args.wait_gpu, on_busy="exit")
    records = []
    try:
        scratch = run_eval.isolate_state()
        brain = conversation.get_brain()
        with time_parse.fixed_clock(CLOCK):
            for rnd in range(1, args.rounds + 1):
                for case in CASES:
                    run_eval.reseed_tasks(scratch)
                    timers.cancel_timer("all")
                    tool_set = {"add_task", "set_timer"} if "minutes" in case else {"add_task"}
                    spec = {"id": case["id"], "prompt": case["prompt"],
                            "expect": {} if case.get("ask") else {"any_tool_of": tool_set}}
                    result = run_eval.run_case(brain, spec, [])
                    store = tasks.list_tasks()
                    running = timers.active()
                    fails = grade(case, result, store, running)
                    timers.cancel_timer("all")
                    rec = {"round": rnd, "id": case["id"], "passed": not fails, "failures": fails,
                           "tools": result["tools"], "reply": result["reply"],
                           "dues": [t["due"] for t in store], "seconds": result["seconds"]}
                    records.append(rec)
                    print(json.dumps(rec, ensure_ascii=False), flush=True)
    finally:
        if lease:
            lease.release()

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    passed = sum(r["passed"] for r in records)
    for case in CASES:
        got = [r["passed"] for r in records if r["id"] == case["id"]]
        print(f"{case['id']:22} {sum(got)}/{len(got)}")
    print(f"PASS {passed}/{len(records)}", flush=True)
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
