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
     set(), {"run_shell_command", "play_song", "search_vault"}),
    ("vault_deadline", [user("when is computational methods coursework 1 due?")],
     {"search_vault"}, set()),
    ("vault_lecturer", [user("who lectures electromagnetism?")],
     {"search_vault"}, set()),
    ("vault_decision", [user("what did we decide about the reminder card?")],
     {"search_vault"}, set()),
    ("vault_again", [user("what is convolution again?")], {"search_vault"}, set()),
    ("greet_no_vault", [user("hey")], set(), {"search_vault"}),
]

# Every model-eval case that needs a tool gets a routing check for free, so
# a new case in eval/cases.py can't fail nightly just because the router
# never offered the tool it needs -- that shows up here, in a second.
from eval.cases import CASES as EVAL_CASES  # noqa: E402

for _c in EVAL_CASES:
    _want = _c["expect"].get("any_tool_of")
    if _want:
        # (id, messages, must all be offered, must not be, at least one of)
        CASES.append((f"eval:{_c['id']}",
                      list(_c.get("before", [])) + [user(_c["prompt"])],
                      set(), set(), set(_want)))
    if _c["expect"].get("no_tools"):
        CASES.append((f"eval:{_c['id']}", [user(_c["prompt"])],
                      set(), {"recall", "search_vault", "get_news"}))

# search_vault only exists while DENISSE_VAULT_SEARCH is on; drop its cases
# otherwise so the control arm still runs clean.
if "search_vault" not in {d["function"]["name"] for d in brain.TOOL_DEFINITIONS}:
    CASES = [c for c in CASES
             if "search_vault" not in c[2] and c[4:] != ({"search_vault"},)]


def names(defs):
    return {d["function"]["name"] for d in defs}


def main() -> int:
    everything = brain.TOOL_DEFINITIONS
    full_size = len(json.dumps(everything))
    failures = 0
    sizes = []

    for cid, msgs, need, ban, *rest in CASES:
        any_of = rest[0] if rest else None
        offered = tool_router.select(everything, msgs)
        got = names(offered)
        sizes.append(len(json.dumps(offered)))
        problems = []
        if need - got:
            problems.append(f"missing {sorted(need - got)}")
        if any_of and not (any_of & got):
            problems.append(f"offers none of {sorted(any_of)}")
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
