"""
Pick which tools Denisse gets to see on a given turn.

Every request used to carry every tool definition -- 31 of them, about
half the prompt. The prompt-budget comment in brain.py records what that
costs on an 8B model: attention is spread across everything in context,
so text that isn't relevant to this turn is paid for by the text that is
(the 2026-09-23 A/B went 10/10 -> 8/10 from one extra paragraph). Tool
schemas that can't possibly apply -- Spotify controls for "what's on my
plate?", startup-program management for "hey" -- are that same cost.

So each turn gets:

  CORE    a small set that is always offered: time, maths, saving a fact,
          the skills library. Cheap, and needed by too many kinds of
          request to gate on keywords.
  groups  everything else, offered only when the recent user messages
          contain one of the group's keywords.

Tools that aren't listed anywhere here (e.g. one added to brain.py later)
are always offered, so a new tool can never silently go missing because
someone forgot to route it. Over-including is safe; under-including is
the failure that matters, so keywords lean broad.

Matching uses the newest user message. If that names nothing but reads
like a follow-up ("skip that one", "and tomorrow?"), the previous user
message is used instead, so the tools it needed carry over.

DENISSE_TOOL_ROUTER=0 turns routing off (every tool, every turn) -- the
control arm for re-running the A/B in eval/run_eval.py.
"""

import os
import re

import instincts

CORE = {
    "get_current_time",
    "calculate",
    "remember",
    "list_skills",
    "read_skill",
}

# group name -> (tools, keyword pattern). Patterns are matched
# case-insensitively against the recent user text.
GROUPS = {
    "files": (
        {"run_shell_command", "open_application", "list_directory",
         "find_files", "read_file", "write_file"},
        r"file|folder|director|pdf|docx?\b|txt|csv|xlsx|pptx|path|desktop"
        r"|download|document|read|write|save|create|find|locate|search|look for"
        r"|where('?s| is| are| did)|open|launch|start|run|command|terminal"
        r"|powershell|cmd\b|app\b|apps\b|application|program|install",
    ),
    "music": (
        {"play_song", "pause_music", "resume_music", "skip_song",
         "previous_song", "current_track", "open_application"},
        r"music|song|track|play|pause|resume|skip|next one|previous|spotify"
        r"|album|artist|listen|tune|banger|stop (it|this|that)|put on",
    ),
    "tasks": (
        {"add_task", "list_tasks", "complete_task", "remove_task"},
        r"task|to-?do|remind|reminder|deadline|due|plate|have to|need to"
        r"|got to|gotta|finish|done|complete|tick|cross (it |that )?off"
        r"|agenda|schedule|by (monday|tuesday|wednesday|thursday|friday"
        r"|saturday|sunday|tomorrow|tonight)|outstanding|pending",
    ),
    # Weather and news used to be one 'info' group, so "do you like rainy
    # days?" was offered get_news and called it (eval small_talk,
    # 2026-10-18). Split so each word only brings its own tool.
    "weather": (
        {"get_weather"},
        r"weather|rain|temperature|forecast|hot\b|cold\b|sunny|umbrella"
        r"|snow|wind|humid|degrees",
    ),
    "news": (
        {"get_news"},
        r"news|headline|happening|current events|going on in",
    ),
    "system": (
        {"get_system_status", "clean_temp_files", "list_startup_programs",
         "disable_startup_program", "restore_startup_program",
         "run_shell_command"},
        r"disk|storage|space|memory|ram\b|cpu|slow|laggy|laptop|computer"
        r"|\bpc\b|status|temp\b|temp files|clean|startup|boot|login|log in"
        r"|optimi[sz]e|performance|speed up|battery",
    ),
    "facts": (
        {"recall", "list_known_facts"},
        r"remember|recall|forget|know about me|you know|do you know"
        r"|what'?s my|what is my|what are my|my fav|favou?rite|about me"
        r"|my name|who am i"
        # A question about his own habits is a facts question even without
        # 'my': "which code editor do I use?" matched nothing before
        # 2026-10-18, so recall was never even offered for it.
        r"|\b(do|did|have|am) i\b|\bi (use|like|prefer|love|hate|usually)\b",
    ),
    # search_vault was the one tool still offered on every turn (it was
    # added before it had a group), and the eval caught a greeting calling
    # it under replayed history. Keywords follow its own description: his
    # modules, lectures, deadlines, decisions, setup, societies -- plus
    # "again" for "what is convolution again".
    "notes": (
        {"search_vault"},
        r"note|obsidian|vault|module|lecture|tutorial|seminar|coursework"
        r"|\bcw\b|assignment|exam|deadline|due|week \d|semester|term\b"
        r"|decid|decision|why did we|set ?up|society|societies|msoc|robotics"
        r"|keats|again|remind me what|explain",
    ),
    # Added with timers.py, 2026-10-20. "start timer" used to match only
    # "files" (on "start"), so the closest thing on offer was
    # run_shell_command -- which it called with "start".
    "timer": (
        {"set_timer", "cancel_timer", "list_timers"},
        r"timer|countdown|count down|stopwatch|time me|\bin \d+\s*(?:sec|min|hour)"
        r"|\b\d+\s*(?:s|sec|second|m|min|minute|h|hour)s?\b.*\b(?:remind|ping|tell|alert)"
        r"|how long (?:is )?left"
        # A bare length is the answer to "how long?" -- "5 minutes" right
        # after "start timer" used to route to nothing, so she called
        # calculate(300) and said "Timer set for 5 minutes" (2026-10-20).
        r"|^\s*(?:for\s+|like\s+|about\s+)?\d+(?:\.\d+)?\s*(?:s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)\s*$",
    ),
    "self": (
        {"get_voice_settings", "get_brain_location"},
        r"voice|sound like|speak|accent|your brain|your code|your memory"
        r"|your files|where (do you|are you)|stored|source code",
    ),
}

_COMPILED = {name: re.compile(pat, re.IGNORECASE)
             for name, (_tools, pat) in GROUPS.items()}

_ROUTED = CORE.union(*(tools for tools, _pat in GROUPS.values()))


def enabled() -> bool:
    return os.environ.get("DENISSE_TOOL_ROUTER", "1").lower() not in (
        "0", "false", "off", "no")


def _user_texts(messages: list, n: int = 2) -> list:
    """The last `n` user messages, newest first."""
    texts = []
    for m in reversed(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            texts.append(m["content"])
            if len(texts) == n:
                break
    return texts


# Words that make a short message lean on the one before it: "and
# tomorrow?", "skip that one", "do it again". A message without any of
# these is treated as a fresh topic, so "hey" after a news question does
# not inherit the news tools -- the eval's live condition caught exactly
# that (greet_hey calling get_news with real history replayed).
_FOLLOW_UP = re.compile(
    r"\b(it|this|that|those|them|one|again|another|more|too|also|and|then"
    r"|instead|same)\b",
    re.IGNORECASE,
)


def _groups_in(text: str) -> list:
    return [name for name, rx in _COMPILED.items() if rx.search(text)]


def matched_groups(messages: list) -> list:
    """Names of the groups this turn needs.

    The newest user message decides on its own when it names something.
    Only when it names nothing *and* reads like a follow-up does the
    previous user message get a say.
    """
    texts = _user_texts(messages)
    if not texts:
        return []
    found = _groups_in(texts[0])
    if found or len(texts) < 2 or not _FOLLOW_UP.search(texts[0]):
        return found
    return _groups_in(texts[1])


def select(tool_definitions: list, messages: list) -> list:
    """Return the subset of `tool_definitions` to offer for this turn.

    Order is preserved, so the model sees tools in the same order as
    before -- only fewer of them.
    """
    if not enabled():
        return tool_definitions

    allowed = set(CORE)
    for name in matched_groups(messages):
        allowed |= GROUPS[name][0]
    # What she learned from being corrected (instincts.py): adds the tool
    # that worked last time, and -- once seen enough -- drops the one that
    # didn't. Only ever changes this list, never the prompt.
    texts = _user_texts(messages, 1)
    if texts:
        allowed = instincts.adjust(allowed, texts[0], protected=CORE)

    def name_of(defn):
        return defn.get("function", {}).get("name")

    return [d for d in tool_definitions
            if name_of(d) in allowed or name_of(d) not in _ROUTED]
