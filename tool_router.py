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
          request to gate on keywords. Gating the skills library instead
          was measured on 2026-10-24 and not kept -- see SKILLS_IN_CORE.
  groups  everything else, offered only when the recent user messages
          contain one of the group's keywords.

Tools that aren't listed anywhere here (e.g. one added to brain.py later)
are always offered, so a new tool can never silently go missing because
someone forgot to route it. Over-including is safe; under-including is
the failure that matters, so keywords lean broad.

Matching uses the newest user message. Short follow-ups can look back
through six user messages for their topic, stopping at a fresh topic.
Complaints about a reply never receive machine-changing tools.

DENISSE_TOOL_ROUTER=0 turns routing off (every tool, every turn) -- the
control arm for re-running the A/B in eval/run_eval.py.
"""

import os
import re

import brain_rules
import instincts

CORE = {
    "get_current_time",
    "calculate",
    "remember",
}

# The skills library is in CORE on the reasoning quoted above -- "needed by
# too many kinds of request to gate on keywords". The hypothesis on
# 2026-10-24 was that this is also why she reaches for a tool on turns that
# want none: on 2026-10-22 `small_talk` ("do you like rainy days?") called
# read_skill, and greetings had picked up list_skills/read_skill in the
# reverted 2026-10-21 prompt experiment too. A tool offered on every turn is
# a tool available on every turn.
#
# Measured, 3 rounds per arm, arms alternating (logs/ab_skills.json):
#
#   in CORE (control)   clean 75/81   live 69/81   combined 144/162
#   gated (test)        clean 76/81   live 71/81   combined 147/162
#
# The aggregate favours gating by 3 in 162, which is inside this suite's own
# noise -- identical code gave clean 25/live 26 and clean 26/live 25 on
# 2026-10-22. Underneath it, two real effects that nearly cancel:
#
#   small_talk  6/6 failures -> 0/6     (gating fixed it outright)
#   greetings   3/12         -> 7/12    (greet_morning started deflecting)
#
# So the reflex did not go away, it moved -- the same thing the 2026-10-21
# prompt experiment did when the deflection shifted from small_talk to
# greet_hey. Gating is NOT kept as the default: "I didn't catch that" in
# answer to "good morning" is a worse failure than a stiff answer about
# rainy days, because he says good morning to her most days and asks about
# rainy days roughly never. A count that improves while the user-visible
# failure gets more frequent is not an improvement.
#
# Conclusion for whoever picks this up: tool routing is the wrong lever for
# the low-content turn. The deflection is conserved across whatever is in
# CORE, so the next attempt should be before the model (a matcher, as
# study_mode does) or in the examples (as _STYLE_EXAMPLES does), not here.
#
# DENISSE_SKILLS_CORE=0 gates them behind the "skills" group below -- the
# test arm. Keep the switch so this can be re-measured when the skill set
# changes; re-running it unchanged just reproduces the numbers above.
SKILLS_IN_CORE = os.environ.get("DENISSE_SKILLS_CORE", "1").lower() not in (
    "0", "false", "off", "no")
if SKILLS_IN_CORE:
    CORE |= {"list_skills", "read_skill"}

# group name -> (tools, keyword pattern). Patterns are matched
# case-insensitively against the recent user text.
GROUPS = {
    "files": (
        {"run_shell_command", "open_application", "list_directory",
         "find_files", "read_file", "write_file"},
        r"file|folder|director|pdf|docx?\b|txt|csv|xlsx|pptx|path|desktop"
        r"|\bcv\b|\br[eé]sum[eé]\b|passport|invoice|pull up|bring up"
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
        r"|keats|remind me what|explain|my projects|current projects|current context"
        r"|what am i working on|catch up with me|(?:going on|happening) with me",
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
        r"|your files|where (do you|are you)|stored|source code|your rules|brain rules|rules (?:do you|you) follow"
        r"|are you learning|learning status",
    ),
    # Only live when DENISSE_SKILLS_CORE=0; with the default these tools
    # are in CORE as well and this group is redundant. It is kept so the
    # 2026-10-24 experiment can be re-run with one env var rather than
    # rebuilt from scratch. Keywords lean broad on purpose, per the
    # docstring: an over-offered skill costs a little attention, a missing
    # one costs the answer. Covers both ways of asking -- naming a skill's
    # subject, and asking for a procedure. Known gap if this is ever made
    # the default: it matches subject names ("biology") but not the terms
    # inside them, so "mitosis vs meiosis" routes to nothing.
    "skills": (
        {"list_skills", "read_skill"},
        r"skill|how (do|should|can) (i|you|we)|teach me|show me how"
        r"|walk me through|step by step|guide|procedure|instructions"
        r"|briefing|brief me|biology|chemistry|physics|python|code\b"
        r"|organi[sz]e|tidy|sort out|take notes?|note-?taking|summar",
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
    r"|instead|same|meant)\b",
    re.IGNORECASE,
)

# These are complaints about the last answer, not new tasks or facts.
# Keep this narrow: "no, add a reminder" is still a real new instruction.
_FEEDBACK_ONLY = re.compile(
    r"^\s*(?:your\s+['\"]?done['\"]?\s+(?:is|means)\s+nothing"
    r"|what(?:'?s| is)\s+done|you\s+(?:didn'?t|did not)\s+(?:do\s+)?anything"
    r"|(?:that|it)\s+(?:didn'?t|did not)\s+work|not\s+what\s+i\s+(?:asked|meant)"
    r"|that'?s\s+(?:wrong|not right)|you tell me|wrong|nope)[.!?\s]*$",
    re.IGNORECASE,
)

MUTATING_TOOLS = brain_rules.MUTATING_TOOLS


def feedback_only(text: str) -> bool:
    return bool(_FEEDBACK_ONLY.fullmatch(text or ""))


def _groups_in(text: str) -> list:
    return [name for name, rx in _COMPILED.items() if rx.search(text)]


def request_context(messages: list) -> tuple[str, list]:
    """Resolve short follow-ups through up to six user turns.

    Stop at a fresh topic even when it has no tools (e.g. a greeting).
    Generic retry words must not create a new notes/tasks/system topic.
    """
    for text in _user_texts(messages, brain_rules.load().recent_turns):
        complaint = feedback_only(text)
        found = [] if complaint else _groups_in(text)
        continuation = complaint or bool(_FOLLOW_UP.search(text))
        routed_text = text
        if continuation and found == ["system"]:
            # "it's in my laptop, try again" still refers to the file.
            specific = re.sub(r"\b(?:laptop|computer|pc)\b", "", text, flags=re.I)
            found = _groups_in(specific)
            routed_text = specific
        if found:
            return routed_text, found
        if not continuation:
            break
    return "", []


def matched_groups(messages: list) -> list:
    return request_context(messages)[1]


def select(tool_definitions: list, messages: list) -> list:
    """Return the subset of `tool_definitions` to offer for this turn.

    Confirmed lessons may prioritise their tools; other order is preserved.
    """
    rules = brain_rules.load()
    allowed = set(CORE)
    for name in matched_groups(messages):
        allowed |= GROUPS[name][0]
    # Confirmed corrections can add read-only tools and prioritise them.
    # No learned text enters the prompt, and no existing tool is removed.
    texts = _user_texts(messages, 1)
    if texts:
        allowed = instincts.adjust(allowed, texts[0], protected=CORE)

    def name_of(defn):
        return defn.get("function", {}).get("name")

    complaint = bool(texts and feedback_only(texts[0]))
    may_save = rules.save_facts == "conversation" or bool(texts and brain_rules.explicit_memory_request(texts[0]))
    selected = [d for d in tool_definitions
            if (not complaint or name_of(d) not in MUTATING_TOOLS)
            and (name_of(d) != "remember" or may_save)
            and (name_of(d) != "search_vault" or rules.vault_search)
            and (not enabled() or name_of(d) in allowed or name_of(d) not in _ROUTED)]
    preferred = instincts.preferred(texts[0], {name_of(d) for d in selected}) if texts else set()
    return sorted(selected, key=lambda d: name_of(d) not in preferred)
