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

The groups, their cue patterns and the core set are data in
tool_policy.json (since 2026-11-03, AGENTS.md Rule 0): edit them there. The
A/B history below is kept because it explains the shape of that data.

Offering is a budget decision, not a permission: a read-only tool the
model calls without being offered still runs (brain.execute,
tool_policy.allowed_unoffered). On 2026-11-03 "open my signals and systems
syllabus" had find_files refused as "not available for this request"
because the router had computed the offered set from keywords.

DENISSE_TOOL_ROUTER=0 turns routing off (every tool, every turn) -- the
control arm for re-running the A/B in eval/run_eval.py.
"""

import os
import re

import brain_rules
import instincts
import tool_policy

CORE = tool_policy.core()

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
    CORE |= tool_policy.skills_tools()

# group name -> (tools, compiled cue pattern), from tool_policy.json
# ("groups"). Cues are matched case-insensitively against the recent user
# text. Why each group's cues are what they are, in the order the file
# lists them:
#
# weather / news: used to be one 'info' group, so "do you like rainy
#   days?" was offered get_news and called it (eval small_talk,
#   2026-10-18). Split so each word only brings its own tool.
# facts: a question about his own habits is a facts question even without
#   'my' -- "which code editor do I use?" matched nothing before
#   2026-10-18, so recall was never even offered for it.
# notes: search_vault was the one tool still offered on every turn (it was
#   added before it had a group), and the eval caught a greeting calling
#   it under replayed history. Cues follow its own description: his
#   modules, lectures, deadlines, decisions, setup, societies. 2026-10-29:
#   "hows my internship tracker" and "the projects im doing" weren't
#   routed here, so she said she had no information. 2026-11-03: "which
#   week of signals did we do the laplace transform" has no "week N", so
#   search_vault was never offered; asking WHICH week is asking his notes.
#   (Bare module names were tried and dropped: "remind me on Thursday to
#   revise signals" is a reminder, and got the vault offered.)
# timer: added with timers.py, 2026-10-20. "start timer" used to match
#   only "files" (on "start"), so the closest thing on offer was
#   run_shell_command -- which it called with "start". A bare length is
#   the answer to "how long?" -- "5 minutes" right after "start timer"
#   used to route to nothing, so she called calculate(300) and said
#   "Timer set for 5 minutes".
# skills: only live when DENISSE_SKILLS_CORE=0; with the default these
#   tools are in CORE as well and this group is redundant. Kept so the
#   2026-10-24 experiment can be re-run with one env var. Cues lean broad
#   on purpose: an over-offered skill costs a little attention, a missing
#   one costs the answer. Known gap if this is ever made the default: it
#   matches subject names ("biology") but not the terms inside them.
# study: the pomodoro's tools (2026-11-03). "timer" and "session" are cues
#   here too, so a typo'd "pause sutuy timer" still gets them offered.
# preferences: the reply_preference tool, for "from now on ..." wordings
#   the fast path in brain.py doesn't catch.
GROUPS = tool_policy.groups()

_COMPILED = {name: rx for name, (_tools, rx) in GROUPS.items()}

# look_at_this (desktop_context.py) reads the selection/clipboard. Offered
# only when the newest message points at something -- "explain this",
# "what does that error mean", "translate the selected text" -- and kept
# out of GROUPS on purpose: "this"/"that" are also the follow-up words
# request_context() looks back through, and as a group they would end that
# look-back early ("and that?" after a weather question would lose the
# weather tools). Newest message only, so it never lingers either.
DESKTOP_TOOLS = tool_policy.desktop_tools()

_ROUTED = CORE.union(DESKTOP_TOOLS, *(tools for tools, _pat in GROUPS.values()))


def points_at_something(text: str) -> bool:
    import desktop_context
    return desktop_context.has_cue(text)


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
_FOLLOW_UP = tool_policy.cue("follow_up")

# These are complaints about the last answer, not new tasks or facts.
# Keep this narrow: "no, add a reminder" is still a real new instruction.
_FEEDBACK_ONLY = tool_policy.cue("feedback_only")

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
    if texts and points_at_something(texts[0]) and not feedback_only(texts[0]):
        allowed |= DESKTOP_TOOLS
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
