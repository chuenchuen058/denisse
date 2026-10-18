"""
Deterministic checks for tool_router -- no model, runs in well under a
second, so it can run on every change rather than nightly.

For each case: the tools the turn needs must be offered, and the tools it
must not reach for must not be. Then prints how much smaller the tool
payload is than sending everything.

    .venv\\Scripts\\python.exe eval\\test_router.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402
import tool_router  # noqa: E402


def user(text):
    return {"role": "user", "content": text}


def bot(text):
    return {"role": "assistant", "content": text}


# (id, messages, must be offered, must NOT be offered)
CASES = [
    ("greet_hey", [user("hey")], set(), {"recall", "get_news", "find_files"}),
    ("greet_thanks", [user("thanks")], set(), {"recall", "list_directory"}),
    ("greet_after_news",
     [user("what's in the news?"), bot("..."), user("hey")],
     set(), {"get_news"}),
    ("time", [user("what time is it?")], {"get_current_time"}, set()),
    ("math", [user("what is 17 times 23?")], {"calculate"}, set()),
    ("find_file", [user("find my japan flight pdf")], {"find_files"}, set()),
    ("home_dir", [user("what folders are in my home directory?")],
     {"list_directory"}, set()),
    ("task_add", [user("remind me to call the dentist tomorrow at 3pm")],
     {"add_task"}, set()),
    ("task_list", [user("what's on my plate?")], {"list_tasks"}, set()),
    ("task_done", [user("I've ordered the N20 motor, tick it off")],
     {"complete_task"}, set()),
    ("music_play", [user("play Blinding Lights")], {"play_song"}, set()),
    ("music_follow_up",
     [user("play some music"), bot("..."), user("skip that one")],
     {"skip_song"}, set()),
    ("weather", [user("is it going to rain in Penang?")], {"get_weather"}, set()),
    ("weather_follow_up",
     [user("what's the weather in London?"), bot("..."), user("and tomorrow?")],
     {"get_weather"}, set()),
    ("news", [user("what's the latest news?")], {"get_news"}, set()),
    ("system", [user("my laptop feels slow, check the cpu")],
     {"get_system_status"}, set()),
    ("recall", [user("what's my favourite coffee?")], {"recall"}, set()),
    ("open_app", [user("open spotify")], {"open_application"}, set()),
    ("garbled", [user("the do way or take a bite white the way")],
     set(), {"get_news", "play_song", "run_shell_command"}),
    ("general", [user("what's the boiling point of water in fahrenheit?")],
     set(), {"run_shell_command", "play_song"}),
]


def names(defs):
    return {d["function"]["name"] for d in defs}


def main() -> int:
    everything = brain.TOOL_DEFINITIONS
    full_size = len(json.dumps(everything))
    failures = 0
    sizes = []

    for cid, msgs, need, ban in CASES:
        offered = tool_router.select(everything, msgs)
        got = names(offered)
        sizes.append(len(json.dumps(offered)))
        problems = []
        if need - got:
            problems.append(f"missing {sorted(need - got)}")
        if ban & got:
            problems.append(f"offered banned {sorted(ban & got)}")
        failures += bool(problems)
        print(f"[{'FAIL' if problems else 'OK  '}] {cid:18} "
              f"{len(offered):2}/{len(everything)} tools  {'; '.join(problems)}")

    avg = sum(sizes) / len(sizes)
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed. Tool payload: "
          f"{full_size} chars unrouted, {avg:.0f} avg routed "
          f"({100 * (1 - avg / full_size):.0f}% smaller).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
