"""
Deterministic checks for learning.py -- no model, no real history.

The fixtures are shaped on real turns from memory/conversation.json
(2026-10-19), reworded where they'd carry personal detail. Each case is a
short history and the label its first turn must get.

    .venv\\Scripts\\python.exe eval\\test_learning.py
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import learning  # noqa: E402


def user(text):
    return {"role": "user", "content": text}


def bot(text):
    return {"role": "assistant", "content": text}


def call(*names):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": n, "arguments": {}}} for n in names]}


def result(text):
    return {"role": "tool", "content": text}


CASES = [
    # (id, history, expected label of the first turn)
    ("false-success", [
        user("study time"), call("read_skill"),
        result("No skill named 'study'. Use list_skills to see what's available."),
        bot("You can use the Pomodoro Timer skill by following these steps..."),
    ], "bad"),
    ("all-tools-failed", [
        user("turn it off"), call("pause_music", "pause_music"),
        result("Spotify doesn't see an active device -- make sure the Spotify app is open."),
        result("Spotify doesn't see an active device -- make sure the Spotify app is open."),
        bot("Spotify is paused."),
    ], "bad"),
    ("admitted-failure", [
        user("pause the music"), call("pause_music"),
        result("Spotify doesn't see an active device -- make sure the Spotify app is open."),
        bot("I couldn't pause it -- Spotify doesn't have an active device open."),
    ], "unknown"),
    ("corrected", [
        user("turn it off"), call("pause_music"), result("Paused."), bot("Paused."),
        user("i meant the timer, turn it off"),
    ], "bad"),
    ("thanked", [
        user("what is 17 times 23?"), call("calculate"), result("391"), bot("391."),
        user("thanks"),
    ], "good"),
    # "no worries" is not a correction.
    ("no-worries", [
        user("hey"), bot("Doing alright, you?"), user("no worries, all good"),
    ], "unknown"),
    # Tools worked and he moved on -- that's not approval. A real turn like
    # this one got "Your task list is still there" for a new reminder.
    ("silence-is-not-approval", [
        user("remind me to reply to sam tonight"), call("list_tasks"),
        result("2 open: ..."), bot("Your task list is still there."),
        user("what's the weather"),
    ], "unknown"),
    # A news headline containing "error" is not a failed tool.
    ("headline-not-failure", [
        user("news"), call("get_news"),
        result("Today's CNA headlines: Umpire error decides final [Sport]"),
        bot("Here are today's headlines."),
    ], "unknown"),
]


def main() -> int:
    failures = 0
    for cid, history, want in CASES:
        turns = learning.split_turns(history)
        nxt = turns[1]["user"] if len(turns) > 1 else None
        verdict, reason = learning.label(turns[0], nxt)
        ok = verdict == want
        failures += not ok
        print(f"[{'OK  ' if ok else 'FAIL'}] {cid:24} {verdict:7} {reason}"
              + ("" if ok else f"  (wanted {want})"))

    # Grouping: the same failing prompt twice is one entry with a count, and
    # the output file is written where it's told to.
    repeat = CASES[0][1] + CASES[0][1]
    out = Path(tempfile.mkdtemp(prefix="denisse-learn-")) / "learned.json"
    got = learning.run(repeat, out_file=str(out))
    ok = (len(got["failures"]) == 1 and got["failures"][0]["count"] == 2
          and out.exists())
    failures += not ok
    print(f"[{'OK  ' if ok else 'FAIL'}] {'groups-repeats':24} "
          f"{len(got['failures'])} entry, count {got['failures'][0]['count'] if got['failures'] else 0}")

    total = len(CASES) + 1
    print(f"\n{total - failures}/{total} passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
