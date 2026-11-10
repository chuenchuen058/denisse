"""
The local brain: talks to a model running on this machine via Ollama
(http://localhost:11434), defines the tool schema, and runs the tool-use
loop that turns a user message into a final text reply (after any tool
calls).

No API key, no internet required after the model is downloaded once.
"""

import copy
import datetime
import inspect
import json
import logging
import ntpath
import os
import platform
import re

import ollama
from dotenv import load_dotenv

import local_llm

import learning
import brain_rules
import desktop_context
import model_config
import model_routing
import reply_preferences
import settings
import speech_control
import tool_policy
import tool_router
import tools
# obsidian_sync is intentionally not imported -- its read_claude_memory()
# is currently disabled below (measurably broke reply quality); re-add
# this import if that's ever re-enabled.

load_dotenv()

# config.json's "chat" model; DENISSE_MODEL still wins (model_config.py), and
# model_routing.json's denisse_brain entry, when set, picks another one.
MODEL = model_routing.model_for("denisse_brain", entry="chat")
# The job name every model call here gives local_llm (its lease policy: local_llm.json lease.by_job).
LLM_JOB = "denisse_brain"

# What this module says, matches and limits -- the reply nets, the requests
# answered before the model, her fixed wording, every threshold -- is data
# in brain.json (AGENTS.md Rule 0), read by brain_rules.data() and checked
# against brain.defaults.json. configure() below rebinds the module values
# from it; tests call it with their own file. The comments by each value
# here say why it exists; the data file says what it is.
CONFIG: dict = {}

# Ollama defaults this model to a 4096-token context, which is small enough
# that what goes into the prompt is a real constraint (see conversation.py
# and the 2026-09-04 history-poisoning bug). Raised to 8192 on 2026-09-05
# after measuring it on this machine with eval/run_eval.py:
#
#   4096  clean 8/8, live 7/8 (twice)   median 2.1s
#   8192  clean 8/8, live 8/8           median 2.0s
#
# The extra KV cache costs ~0.5GB: 5.8GB of the 8151MB laptop GPU, still
# 100% on GPU with ~2GB free, no CPU spill and no measured slowdown. The
# remaining live failure at 4096 ("hey" answered with a get_current_time
# call) stopped reproducing at 8192.
#
# Don't raise this further without re-running that eval -- the headroom is
# small, and if it ever spills to CPU every reply gets dramatically slower.
# The value is brain.json limits.num_ctx; DENISSE_NUM_CTX still wins.
NUM_CTX = 8192

# How many times one turn may go round the tool-use loop before Denisse has
# to answer in words. The loop used to be `while True` -- a model that keeps
# asking for tools never terminated, and every round appends the call and
# its result to the request, so the prompt grows until it fills NUM_CTX and
# Ollama starts truncating it.
#
# That is not hypothetical: the 2026-09-12 eval run started and never
# finished, and the 2026-09-19 run was still going after ten minutes, with
# Ollama logging ~8200-token requests at ~6s each (vs ~2s when the prompt
# was small). Bounded at 6 -- deep enough for a genuine multi-step answer,
# shallow enough that a stuck turn costs seconds rather than forever. On
# the final round tools are withheld entirely, which forces a text reply.
# The value is brain.json limits.max_tool_rounds; DENISSE_MAX_TOOL_ROUNDS
# still wins.
MAX_TOOL_ROUNDS = 6


def _env_int(name: str, default: int) -> int:
    """An env override of a data value (kept from before brain.json)."""
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _chat_extra() -> dict:
    """Keyword arguments every ollama.chat call takes from brain.json chat (think)."""
    think = CONFIG["chat"].get("think")
    return {} if think is None else {"think": think}


def _chat_options() -> dict:
    return {"temperature": CONFIG["chat"]["temperature"], "num_ctx": NUM_CTX}


_NEVER = re.compile(r"(?!x)x")  # matches nothing


def _rx(pattern, flags=re.IGNORECASE) -> re.Pattern:
    """One regex from a brain.json pattern: a string, or a list of
    alternatives joined with |. Empty means it never matches."""
    if isinstance(pattern, list):
        pattern = "|".join(p for p in pattern if p)
    return re.compile(pattern, flags) if pattern else _NEVER


def _first_name() -> str:
    """Whose assistant this is, as she addresses him (settings.py)."""
    try:
        return (settings.user_name().split() or [""])[0]
    except Exception:
        return ""

# The system prompt is brain.json prompt, in parts, joined at configure().
# The machine facts (OS, user, home and program folders, path separator)
# are derived at runtime so they can't drift from the machine she runs on:
# without them the model guessed POSIX paths for file tools on a Windows
# box -- list_directory("/home/chuen"), read_file("/home/user/setup.txt")
# for "what do you think of my setup?". The wording is as tuned; the A/B
# record (prompt._about in the data) says why every sentence must stay as
# it is, and why nothing should be added: the prompt is ~4000 characters
# that an 8B model spreads its attention over, so a sentence added for one
# case is paid for by every other. Guidance for one tool belongs in that
# tool's description, nets under the reply in code (_unleak_json).
SYSTEM_PROMPT = ""


def _system_prompt(p: dict) -> str:
    """brain.json prompt with this machine's facts and the model filled in."""
    example = p["path_example_windows"] if os.name == "nt" else p["path_example_other"]
    facts = p["machine_facts"].format(
        system=platform.system(), release=platform.release(),
        user=os.environ.get("USERNAME") or os.environ.get("USER") or p["unknown_user"],
        home=os.path.expanduser("~"), project=os.path.dirname(os.path.abspath(__file__)),
        sep=os.sep, path_example=example)
    return facts + p["identity"].format(model=MODEL) + p["conduct"]


# The tools the model is offered: brain.json tools.definitions (name,
# description, JSON-schema parameters), in that order, wrapped in Ollama's
# function envelope here. All of a tool's guidance lives in its description,
# never in the prompt (the budget lesson above); tool_router offers each
# group only when the message is about it, so no other turn pays for it.
# What a call may DO is tool_policy.json. A definition with enabled_env is
# left out while that variable says off -- search_vault's
# DENISSE_VAULT_SEARCH=0 is the control arm of the vault A/B (the first
# attempt pasted the vault into the prompt and measured 0/4 clean).
TOOL_DEFINITIONS: list = []
_TOOL_NAMES: set = set()   # every tool name with an underscore (tool talk)
_OFF = ("0", "false", "off", "no")


def _env_on(name: str | None, default: str = "1") -> bool:
    """An on/off environment switch; no name means on."""
    return not name or os.environ.get(name, default).lower() not in _OFF


def _tool_definitions(c: dict) -> list:
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": copy.deepcopy(t["parameters"])}}
            for t in c["tools"]["definitions"] if _env_on(t.get("enabled_env"))]


def _unleak_json(text: str, last_tool_result: str) -> str:
    """Replace a reply that is really a JSON status object with something a
    person would say.

    Observed 2026-09-23: asked to tick a task off, the model answered
    '{"status": "success", "message": "Task completed successfully."}'.
    The tools already return plain English, so the model invented that. The
    system prompt now forbids it, but an 8B model will do it anyway
    sometimes, so this is the net underneath.

    Prefers a human-readable field out of the blob, then the actual tool
    result, and only then a generic line -- in that order, because the tool
    result is the one thing here that is definitely true.
    """
    stripped = (text or "").strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return text
    try:
        blob = json.loads(stripped)
    except (ValueError, TypeError):
        return text
    if not isinstance(blob, dict):
        return text
    for key in CONFIG["json_leak"]["fields"]:
        value = blob.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return (last_tool_result or "").strip() or CONFIG["json_leak"]["fallback"]


# A reply that is nothing but commentary on her own tool use: "No tool call
# needed for that." Seen 2026-10-18 in the eval's live condition, answering
# "thanks" -- twice out of two runs once the router had shrunk the tool list.
# The system prompt already forbids it; like _unleak_json, this is the net
# underneath. Only a reply made ENTIRELY of such a sentence matches, so a
# real answer that happens to mention a tool is never touched.
# "json" since 2026-10-22: "No JSON response is needed for this one." --
# all three greetings in one eval run once _STYLE_EXAMPLES were on.
# Pattern: brain.json reply_nets.tool_narration_pattern (bound by configure).

# "I didn't catch that. Could you rephrase?" to a clear, typed question.
# Traced 2026-10-30: "do you like rainy days?" got it 4/4 against the live
# history and 0/3 clean -- the history confused her, the question was fine.
# conversation._drop_failed now keeps the worst turns out of the replay;
# this is the backstop when it still happens: one retry with the history
# left out. A message of under three words can still get "didn't catch".
# Patterns: reply_nets.didnt_catch_patterns; the word count is
# reply_nets.clear_message_min_words.


def _clear_message(text: str) -> bool:
    nets = CONFIG["reply_nets"]
    return len(_WORD.findall(text or "")) >= nets["clear_message_min_words"]


# "and in Penang?" after a weather question. The router carries the weather
# tool over, but live the model called nothing and answered from memory --
# 3/3 before the 2026-10-30 replay change, still 2/3 after. Only one right
# thing to do, so the code asks for the place. Patterns: brain.json weather.


def _weather_place(text: str, request_groups) -> str | None:
    """The place in a weather follow-up, or None."""
    if "weather" not in (request_groups or ()):
        return None
    m = _PLACE_FOLLOW_UP.match(text or "")
    if not m:
        return None
    place = m.group(1).strip()
    if _NOT_A_PLACE.match(place) or _TIME_REQUEST.search(place):
        return None
    return place


# His standing asks, 2026-10-31, each said more than once in one evening:
# "you dont have to say the file path its too long and not great to hear",
# "dont show the path at all ... ask if i want the file path", "you could
# just say me instead of chuen", "keep it short and simple". The model
# agreed every time and did it again in the next reply, so the code does it
# to every reply instead. Paths become file names plus one question; the
# full paths are kept (Brain._last_paths) for a "yes". Asking for it, not
# mentioning it: "you dont have to say the file path" kept every path in
# the first version (paths.wants_path_patterns). Patterns and wording:
# brain.json paths.


def _hide_paths(text: str) -> tuple[str, list]:
    """Swap every Windows path in a reply for its file name. Returns the new
    text and the paths taken out, in order, without repeats."""
    taken = []
    names = {}
    for m in _WIN_PATH.finditer(text or ""):
        names.setdefault(m.group("name").lower(), set()).add(m.group(0).rstrip(".").lower())

    def where(full, others):
        # The nearest folder that tells same-named files apart: "Resume\IUMC"
        # and "Resume - Copy\IUMC" differ one level up, not at IUMC.
        parts = re.split(r"[\\/]", full.lower())
        rest = [re.split(r"[\\/]", o) for o in others if o != full.lower()]
        for k in range(2, len(parts) + 1):
            if all(len(o) < k or o[-k] != parts[-k] for o in rest):
                return re.split(r"[\\/]", full)[-k]
        return re.split(r"[\\/]", full)[-2]

    def name_of(m):
        full = m.group(0).rstrip(".")
        if full not in taken:
            taken.append(full)
        name = m.group("name")
        same = names.get(name.lower(), ())
        if len(same) > 1:
            # Two "Passport ctc.pdf"s read as a repeat; say where each is.
            return f"{name} (in {where(full, same)})"
        return name

    out = _WIN_PATH.sub(name_of, text or "")
    out = _SEARCH_MISS.sub(CONFIG["paths"]["search_miss_replacement"], out)
    return out, taken


def _second_person_rules(name: str, c: dict) -> list:
    """The substitutions that turn his name into "you" -- built from
    brain.json second_person once per configure(). Each is (regex,
    replacement, only_if_changed)."""
    if not name:
        return []
    n = re.escape(name)
    verbs = c["verbs"]
    verb_alt = "|".join(re.escape(v) for v in verbs)
    not_before = "|".join(re.escape(v) for v in c["possessive_not_before"])
    cues = "|".join(c["verb_cues"])
    lead_ins = "".join(f"(?<!{re.escape(w)} )" for w in c["address_lead_ins"])
    he, you = re.escape(c["subject_pronoun"]), c["subject_replacement"]

    def second(m):
        return f"{you} " + verbs[m.group(1)]

    rules = [
        (re.compile(rf"\b{n}'?s\b(?!\s+(?:{not_before})\b)"), c["possessive_replacement"], False),
        (re.compile(rf"\b{n}\s+({verb_alt})\b"), second, False),
        (re.compile(rf"\b{n}(?=\s+(?:{cues})\b)"), you, False),
        (re.compile(rf"(?<!, ){lead_ins}\b{n}\b(?!,)(?![.!?]?\s*$)"), you, False),
    ]
    rules += [(re.compile(rf"\b{re.escape(p)}\b"), r, True) for p, r in c["pronouns"].items()]
    rules += [
        (re.compile(rf"\b{he}\s+({verb_alt})\b"), second, True),
        (re.compile(rf"\b{he}\b"), you, True),
    ]
    starts = "|".join(re.escape(w) for w in c["capitalise"])
    rules.append((re.compile(rf"(^|[.!?]\s+|\n)({starts})\b"),
                  lambda m: m.group(1) + m.group(2).capitalize(), True))
    return rules


def _second_person(text: str) -> str:
    """Notes say "Chuen is a collaborator"; she reads that out to Chuen.
    Talking about him to him, "Chuen" is "you". "Hey Chuen" and "Thanks,
    Chuen" stay -- those are talking to him already. The name is
    settings.user_name()'s first word; the grammar is brain.json
    second_person."""
    if not _NAME or _NAME not in (text or ""):
        return text
    sentences = re.split(r"(?<=[.!?\n])(\s+)", text)
    out = []
    for s in sentences:
        if _NAME not in s:
            out.append(s)
            continue
        before = s
        for rx, repl, only_if_changed in _SECOND_PERSON:
            if only_if_changed and s == before:
                break
            s = rx.sub(repl, s)
        out.append(s)
    return "".join(out)


def _tidy_reply(text: str, latest_user: str) -> tuple[str, list]:
    """The last step before a reply leaves: his standing style asks, done in
    code. Returns (reply, paths hidden from it)."""
    text = _NOTES_HEADER.sub(CONFIG["notes"]["header_replacement"], text or "")
    text = _second_person(text)
    if _WANTS_PATH.search(latest_user or ""):
        return text, []
    text, paths = _hide_paths(text)
    if paths and _PATH_ASK not in text:
        text = text.rstrip() + "\n\n" + _PATH_ASK
    return text, paths


# What a call may do is tool_policy.json (AGENTS.md Rule 0, 2026-11-03).
# Until then execute() matched regexes against HIS words to decide whether
# a call was allowed -- write_file needed "save"/"create" in his message,
# add_task needed "remind"/"task", the clock needed a time word -- and on
# 3 Nov that refused the file search that would have found his syllabus
# and told him "No tool needed" for "session over". Now the policy checks
# the CALL: its arguments and side effect. The three safety refusals that
# were real -- a destructive shell command, a write into Denisse's own
# folder, opening a document path nothing verified -- are its `checks`.
_REPO_DIR = os.path.dirname(os.path.abspath(__file__))


def _inside_denisse(path: str) -> bool:
    return tool_policy._inside(_REPO_DIR, path)


def _policy_pattern(tool: str, kind: str) -> re.Pattern:
    for chk in tool_policy.tool(tool).get("checks", []):
        if chk["kind"] == kind:
            return re.compile(chk["pattern"], re.IGNORECASE)
    return re.compile(r"(?!x)x")  # matches nothing


# Deleting or moving his files is never a side effect of a chat. On
# 2026-10-31 "yes delete them" produced three Move-Item calls on his CVs
# (stubbed in the replay; run_shell_command runs unasked, safety.py). The
# pattern is run_shell_command's must_not_match check in the policy.
_DESTRUCTIVE_SHELL = _policy_pattern("run_shell_command", "must_not_match")

# How he wants her to talk, not a task: "you dont have to say the file
# path", "from now on ...", "next time you could just ...", "keep it short".
# On 2026-10-31 the model answered these with "I've moved the files to a
# temporary folder", a junk task, and three junk files. He asked for
# "alright noted". The phrases are tool_policy.json cues (reply_preference);
# the note goes to reply_preferences.py, which feeds every later prompt --
# and the model has the same store as a tool for wordings nobody listed.
_STYLE_NOTE = tool_policy.cue("reply_preference")
_STYLE_THEN_REQUEST = tool_policy.cue("reply_preference_then_request")


def _style_note(text: str) -> bool:
    t = (text or "").strip()
    # A reminder is a task even when phrased like a note
    # (cues.style_note_excludes_pattern); "..., but find my cv" carries a
    # real request after the note.
    return (bool(_STYLE_NOTE.match(t)) and not _STYLE_NOTE_EXCLUDES.search(t)
            and not _STYLE_THEN_REQUEST.search(t))


def _write_style_note(text: str) -> str:
    return reply_preferences.add(text)

# A deadline he never said. "remind me to submit the team sign up" became
# "on the 6th of October at 2 PM" (2026-10-31). The words that pin a date
# have to be his. Words and aliases: brain.json dates.


def _due_said(due: str, text: str) -> bool:
    def words(s):
        out = set()
        for w in _DUE_WORDS.findall((s or "").lower()):
            if w.isdigit():
                if int(w):
                    out.add(str(int(w)))
            else:
                out.add(_DUE_ALIAS.get(w, w))
        return out
    return words(due) <= words(text) | _DUE_ALWAYS if words(due) else True


# "before 2nd of november", said straight after she added a task: that's
# the task's deadline. Live she said "I'll add that to your reminders" and
# changed nothing. Pattern: brain.json tasks.deadline_only_pattern.


# A real answer wrapped in commentary on how she got it: "I called the
# get_weather tool to get the current weather in London. According to the
# tool, ..." and "The tool call returned a list of news headlines" (eval,
# 2026-10-20 -- both passed, and the second went into dataset.jsonl).
# _TOOL_NARRATION only matches a reply made entirely of such talk, so these
# got through. Any tool's own name in a reply counts too: Chuen never says
# "current_track", so she has no reason to. Patterns: brain.json
# reply_nets.tool_talk_patterns / tool_lead_in_pattern; _TOOL_NAMES is
# bound by configure() from the tool definitions.


def _talks_about_tools(text: str) -> bool:
    low = (text or "").lower()
    return bool(_TOOL_TALK.search(low)) or any(n in low for n in _TOOL_NAMES)


# Lead-ins that wrap a real answer: "According to the tool, it's raining" and
# "I called the 'current_track' tool to see what's playing, and it said
# nothing is". Cut, the rest is the answer (_strip_tool_talk).

# The chat template's role marker emitted as text: "assistant\n\nNo worries."
# Seen 2026-10-21 on live greetings, old code too. Traced to the
# _PLAIN_REPLY_NUDGE retry: a request ending on a system note gets the
# marker back 3 times out of 3. Lowercase only, as observed
# (reply_nets.role_prefix_pattern).


# Made-up exchanges in her voice, placed between the system prompt and the
# real conversation. 2026-10-22: Chuen wants her to "speak like a real
# person", and she talks in report-voice -- "The Pomodoro Timer has been
# started.", "The result of the multiplication is 391." Rules in
# SYSTEM_PROMPT have twice made her worse (see its comments), but the model
# demonstrably copies the conversation it's shown -- that is how the bad
# history poisoned her on 2026-09-04. This turns that the right way round.
# The exchanges are brain.json style_examples.messages.
def _style_examples() -> list:
    # Read per call, so the eval can A/B it without a restart.
    default = "1" if brain_rules.load().style_examples else "0"
    on = _env_on("DENISSE_STYLE_EXAMPLES", default)
    return [dict(m) for m in CONFIG["style_examples"]["messages"]] if on else []


# A news reply with no news in it: "I've got the latest news for you.",
# "Today's news.", "It looks like the news summary was cut off" -- all real
# (his history and the 2026-10-22 eval), while get_news had returned six
# good headlines. Its description already demands 2-3 named headlines and
# the model ignores that, so -- like _unleak_json -- the net reads them out
# from the tool result, the one thing here that is definitely true.
# Patterns, limits and wording: brain.json news.


def _headlines(result: str) -> list:
    body = _NEWS_HEAD.sub("", (result or "").strip(), count=1)
    if body == (result or "").strip():
        return []           # not a headline list -- an error, say
    heads = [re.sub(r"\s*\[[^\]]*\]\s*\.?$", "", h).strip().rstrip(".")
             for h in body.split(";")]
    return [h for h in heads if h]


def _has_news_content(reply: str, result: str) -> bool:
    """True if the reply uses at least news.min_shared_words distinctive
    words from the headlines -- enough to show it relayed some of them."""
    words = {w for h in _headlines(result)
             for w in _NEWS_WORD.findall(h.lower())}
    if not words:
        return True         # nothing to compare against; leave it alone
    said = set(_NEWS_WORD.findall((reply or "").lower()))
    return len(words & said) >= CONFIG["news"]["min_shared_words"]


def _looked_text(result: str) -> str:
    """The text look_at_this quoted (between its wording open/close marks,
    desktop_context.json), or "" when it quoted nothing."""
    import desktop_context
    say = desktop_context.CONFIG["wording"]
    head, sep, rest = (result or "").partition(say["open"])
    body, end, _ = rest.partition(say["close"])
    return body.strip() if sep and end else ""


def _uses_looked_text(reply: str, text: str) -> bool:
    """Did the reply work on the looked-at text, or only describe it? At
    least looked_text.min_shared_words of its words (min_word_letters long
    or more) in the reply -- fewer when the text itself has fewer."""
    cfg = CONFIG["looked_text"]
    word = re.compile(r"[^\W\d_]{%d,}" % int(cfg["min_word_letters"]))
    words = set(word.findall((text or "").lower()))
    if not words:
        return True  # nothing to compare against; leave it alone
    said = set(word.findall((reply or "").lower()))
    return len(words & said) >= min(int(cfg["min_shared_words"]), len(words))


def _speak_headlines(result: str, n: int | None = None) -> str:
    news = CONFIG["news"]
    heads = _headlines(result)[:news["max_headlines"] if n is None else n]
    if not heads:
        return ""
    heads = [_NEWS_STRIP.sub("", h) for h in heads]
    if len(heads) == 1:
        return news["one"].format(head=heads[0])
    return news["many"].format(heads=". ".join(heads[:-1]), last=heads[-1])


def _strip_role_prefix(text: str) -> str:
    out = _ROLE_PREFIX.sub("", text or "", count=1)
    return out if out.strip() else text


def _strip_tool_talk(text: str) -> str:
    """Cut the narration out of a reply that wraps a real answer in it.

    Deterministic on purpose. The first version re-asked the model with tools
    off, and on 2026-10-21 that turned a good weather answer into "assistant
    I checked the weather for you." and the news into "assistant I didn't
    catch that." -- worse than the narration. So: strip lead-ins, drop the
    sentences that are still tool talk, and if fewer than four words survive
    there was no answer inside it -- return the original untouched.
    """
    if not _talks_about_tools(text):
        return text
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        cut, n = _TOOL_LEAD_IN.subn("", sentence)
        if n and cut:
            cut = cut[0].upper() + cut[1:]
        if cut and not _talks_about_tools(cut):
            kept.append(cut)
    out = " ".join(kept)
    return out if len(out.split()) >= CONFIG["reply_nets"]["min_words_kept"] else text


# A reply that says something was DONE (learning.CLAIMS_DONE, shared with
# the nightly labeller). Seen 2026-10-20: "start timer" ->
# run_shell_command("start") -> "(no output)" -> "The timer has started.",
# then "stop timer" -> no tool at all -> "Done." Neither was true; there was
# no timer. The system prompt already says a tool result is ground truth,
# and an 8B model claims success anyway, so -- like _unleak_json -- this is
# the net underneath, checked against what actually ran this turn. The
# nudges and the two replies it ends on are brain.json honesty.
_CLAIMS_DONE = learning.CLAIMS_DONE

# What the honesty net says when it rejects a done-claim for good. Named so
# multistep.py can tell a rejected step from one that worked. Bound from
# honesty.unconfirmed_reply / failed_reply by configure().
UNCONFIRMED_REPLY = ""
FAILED_REPLY = ""


# A claim about a thing that only certain tools can change. "Timer set for 5
# minutes" after calculate(300) passed the check above -- calculate DID
# succeed, it just isn't a timer (2026-10-20). So a done-claim that names one
# of these also needs one of its tools to have run this turn. He calls the
# pomodoro "the study timer", so "timer paused" after pause_study_session is
# a true claim (eval study_pause_typo, 2026-11-03), and "Sutuy timer
# paused." after pause_music needs a study tool: the pairs are
# honesty.claim_needs. "Deleted 280 old CVs." (2026-10-31) after three
# write_file calls and a refused delete: she has no tool that deletes his
# files, so a claim that files are gone (honesty.delete_claim_pattern) is
# checked on its own, done-word or not.


def _claims_deleted(text: str) -> bool:
    window = CONFIG["honesty"]["negation_window_chars"]
    for m in _DELETE_CLAIM.finditer(text or ""):
        before = text[max(0, m.start() - window):m.start()].lower()
        if not _NEGATED_BEFORE.search(before):
            return True
    return False


def _claim_without_its_tool(text: str, ran: set) -> bool:
    if _claims_deleted(text) and not ran & _DELETE_TOOLS:
        return True
    if not (_CLAIMS_DONE.search(text) or _SET_FOR.search(text)):
        return False
    return any(rx.search(text) and not (ran & needs) for rx, needs in _CLAIM_NEEDS)


def _confirms_nothing(result: str) -> bool:
    """A tool result that is no evidence the thing was done: an error, a
    refusal, or no output at all (run_shell_command("start") returns
    "(no output)" and opens nothing useful). brain.json evidence."""
    r = (result or "").strip()
    ev = CONFIG["evidence"]
    return (not r or r in ev["empty_results"] or learning.tool_failed(r)
            or r.startswith(tuple(ev["no_evidence_prefixes"]))
            or bool(_NO_EVIDENCE.search(r.lower())))


def _has_task_content(reply: str, result: str) -> bool:
    """Don't replace a real task list with 'you have a lot on your plate'."""
    items = re.findall(r"^\s*-\s+(.+)$", result, re.MULTILINE)
    if not items:
        return True
    words = {w for item in items for w in _TASK_WORD.findall(item.lower())}
    words -= _TASK_STOP
    said = set(_TASK_WORD.findall(reply.lower()))
    return bool(words) and len(words & said) >= min(CONFIG["tasks"]["min_shared_words"], len(words))


# ---- 2026-10-29: replies that were wrong 3 runs out of 3 ------------------
# Each piece below replaces a model decision that the eval showed failing
# every time, in both clean and live (eval/cases.py, the 2026-10-26..28
# block; scripts/test_reply_guards.py). The pattern is the one Friday's A/B
# pointed at (decisions.md 2026-10-24): where there is one right answer,
# decide it in code, before or after the model, not by asking it nicely.
# The requests each one matches are brain.json cues; the words it then
# says are brain.json text.

# "Stop telling me the news" asks for the daily briefing to be switched
# off. The word "news" routed every one of these to get_news, and
# _speak_headlines then forced the headlines into the reply even when the
# model hadn't -- so the code itself turned "stop" into a news bulletin.
# Nothing she could call turned the briefing off. Now it does
# (cues.news_off_pattern).

# What she runs on is a fact, not something for the model to work out.
# Asked "how many parameters do you have?" she called calculate and said
# "I have 8 parameters", or list_skills and "I have 8 skills" (3/3 each).
# cues.self_model_patterns; the answer is built from the model tag.


def _model_identity() -> str:
    name, _, tag = MODEL.partition(":")
    m = re.match(r"([a-z]+)([\d.]*)", name, re.IGNORECASE)
    family = (m.group(1).capitalize() + (" " + m.group(2) if m.group(2) else "")) if m else name
    size = re.search(r"(\d+(?:\.\d+)?)\s*b\b", tag, re.IGNORECASE)
    if size:
        return CONFIG["text"]["identity_sized"].format(family=family, n=size.group(1))
    return CONFIG["text"]["identity_plain"].format(model=MODEL)


# calculate is always offered (tool_router.CORE), and on 2026-10-28 she
# called it nine times on things that were not maths: "0" for "stop
# talking", "100" for "how high is your iq", "parameter". Friday's finding
# again -- the tool reflex is conserved and lands on whatever is always on
# offer. Until 2026-11-03 the block was a word list matched against HIS
# message; now it is calculate's must_match check in tool_policy.json, on
# the expression itself: "100" has nothing to calculate, "17*4" does.

# What to search for when she answered a file request without searching.
# Only from "my <thing>" or "called/named <thing>" -- guessing a query out
# of a whole sentence would search for junk. brain.json file_query.


def _file_query(text: str) -> str | None:
    t = (text or "").lower()
    fq = CONFIG["file_query"]
    if _FILE_PATH_GIVEN.search(t):
        return None  # he gave a path; don't turn it into keywords
    m = next((m for m in (rx.search(t) for rx in _FILE_NAMED) if m), None)
    if not m:
        return None
    words = [w for w in _FILE_WORD.findall(m.group(1)) if w not in _FILE_QUERY_STOP]
    return " ".join(words[:fq["max_words"]]) or None


# list_skills ran, and the reply was "I've got a few skills that might
# come in handy" -- four times running on 2026-10-28, 3/3 in the eval.
# Same shape as the task-list restore: fetching the answer and not saying
# it is the failure.
def _skill_names(result: str) -> list:
    return re.findall(r"^([a-z][a-z0-9_]*):", result or "", re.MULTILINE)


# Words in skill names that also turn up in vague replies. The first version
# counted "take" (from take_notes), so "I can take care of a few things"
# passed as naming a skill and the list was never restored (2/3 clean runs,
# 2026-10-29). brain.json skills.generic_words.


def _names_a_skill(reply: str, result: str) -> bool:
    low = (reply or "").lower()
    min_chars = CONFIG["skills"]["min_word_chars"]
    for name in _skill_names(result):
        words = [w for w in name.split("_") if len(w) >= min_chars and w not in _GENERIC_SKILL_WORDS]
        if any(w in low for w in words):
            return True
    return not _skill_names(result)


def _skills_reply(result: str) -> str:
    skills = CONFIG["skills"]
    lines = []
    for name, desc in re.findall(r"^([a-z][a-z0-9_]*):\s*(.+)$", result or "", re.MULTILINE):
        desc = _SKILL_STRIP.sub("", desc.strip())
        lines.append(skills["line"].format(name=name.replace("_", " ").capitalize(), desc=desc))
    return skills["heading"] + "\n" + "\n".join(lines) if lines else result


# ---- 2026-10-29 (afternoon): "actually do the task" ----------------------
# Chuen, after the morning's fixes: "i dont want her to reply me to satisfy
# me i want her to actually do task". Every case below sounded helpful and
# did nothing (eval/cases.py, the block of the same date; 3/3 failures).

# A complaint means "redo what I asked", not "call a tool". Live, "thats not
# what im asking" and "what?" were answered with the time, five times. The
# phrases are the "complaint" cue in tool_policy.json; since 2026-11-03 an
# insult ("omg youre so dumb", which got "What did you mean by that?") is
# one too: say what he asked and redo it.
_COMPLAINT = tool_policy.cue("complaint")
# A bare acknowledgement, skipped when looking for the request a complaint
# is about: cues.ack_pattern.


def _call_key(name: str, args) -> str:
    return json.dumps([name, args or {}], sort_keys=True, default=str)


# A lookup that ran but found nothing. These are the real tools' own words
# (tools.recall, vault_search) and none of them is an "error", so until
# 2026-10-30 _confirms_nothing missed them: live, a retry repeated
# recall('exams') 3/3 against the real wording while the fixtures, which
# said "Error: ...", passed. evidence.found_nothing_patterns.


def _no_evidence(result: str) -> bool:
    return _confirms_nothing(result) or bool(_FOUND_NOTHING.search((result or "").strip()))


# Where else a lookup that found nothing can be checked. Read-only, and
# only tools the original request was offered. One step, not a planner.
# evidence.recovery_sources; the filler words dropped from his request are
# evidence.lookup_filler.


def _lookup_terms(text: str) -> str:
    """The subject of a lookup, from his own words -- never invented."""
    import instincts
    keep = instincts.keywords(text) - _LOOKUP_FILLER
    return " ".join(dict.fromkeys(w for w in _LOOKUP_WORD.findall((text or "").lower()) if w in keep))


def _permitted_reads() -> set:
    """Read-only tools that are defined and switched on."""
    rules = brain_rules.load()
    return {d["function"]["name"] for d in TOOL_DEFINITIONS
            if d["function"]["name"] not in brain_rules.MUTATING_TOOLS
            and (d["function"]["name"] != "search_vault" or rules.vault_search)}


def _lookup_args(name: str, terms: str) -> dict:
    """His words as the one argument a lookup tool takes -- search_vault's
    query, recall's key -- read from the tool's own definition."""
    for d in TOOL_DEFINITIONS:
        if d["function"]["name"] == name:
            params = d["function"].get("parameters") or {}
            arg = next(iter(params.get("required") or params.get("properties") or ()), None)
            return {arg: terms} if arg else {}
    return {}


def _blocked_reply(terms: str, checked: list) -> str:
    """Nothing found anywhere: say where she looked, then one question.
    Wording per tool: brain.json lookup."""
    lookup = CONFIG["lookup"]
    parts = []
    for name, res in checked:
        entry, rx = _LOOKUP_WHERE.get(name, (None, None))
        if entry is None:
            continue
        if rx is not None and rx.search(res):
            parts.append(entry["unreachable"])
        else:
            parts.append(entry["nothing"].format(terms=terms))
    where = "; ".join(dict.fromkeys(parts)) or lookup["nowhere"]
    return lookup["blocked"].format(where=where, terms=terms)


def _corrected_attempt(prev: str, messages: list, prev_i: int) -> list:
    """The calls the complained-about attempt made, with their results:
    from `messages` when they carry tool traffic (tests, the CLI), else
    from the stored history (the tray -- replay() is prose only)."""
    calls, results = [], []
    for m in messages[prev_i + 1:-1]:
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                f = c.get("function") or {}
                if f.get("name"):
                    calls.append({"name": f["name"], "arguments": f.get("arguments") or {}})
        elif m.get("role") == "tool":
            results.append(m.get("content") or "")
    if calls:
        for call, res in zip(calls, results):
            call["result"] = res
        return calls
    try:
        import conversation
        return conversation.last_attempt(prev)
    except Exception:
        return []


def _last_real_request(messages: list) -> int | None:
    """Index of the last thing he actually asked for, skipping the current
    message and any complaints, acknowledgements or stop requests."""
    for i in range(len(messages) - 2, -1, -1):
        m = messages[i]
        if m.get("role") != "user":
            continue
        text = (m.get("content") or "").strip()
        if (not text or _COMPLAINT.fullmatch(text) or _ACK.fullmatch(text)
                or tool_router.feedback_only(text) or speech_control.is_stop_request(text)):
            continue
        return i
    return None


# Time words (tool_policy.json cue "time_words"). Until 2026-11-03 this
# gated get_current_time on his message having one of them, and "session
# over" got "No tool needed here" for it. The clock is the model's choice
# now; the words are still used to tell "and tomorrow?" from a place name
# in a weather follow-up (_weather_place).
_TIME_REQUEST = tool_policy.cue("time_words")

# Weather that no weather tool returned. Live, asked for the briefing:
# "Temperature: 68°F ... Humidity: 60% ... Conditions: Partly Cloudy ...
# Forecast: High of 72°F" -- get_weather was never called. Only replies
# carrying weather *data* words trigger this, so "water boils at 212
# degrees fahrenheit" or "I like sunny days" are left alone. brain.json
# weather.data_patterns / line_patterns.


# The current date or time, said with no clock run. Live (2026-10-30 probe
# of the 28 Oct "alright tell me"): "It's currently December 2023." -- her
# training cutoff, stated as today. A bare number only counts with a clock
# shape (18:48, 6 pm), so "it's 14°C" and "it is 391" are left alone. A day
# or month only counts as "now" when she says so -- "It's Tuesday 24
# November" can be a deadline read from his notes. The pieces are
# brain.json clock; _clock_claim puts them together.


def _clock_claim(c: dict) -> re.Pattern:
    shape, calendar = c["shape_pattern"], c["calendar_pattern"]
    it_is, now, today = c["says_it_is_pattern"], c["says_now_pattern"], c["says_today_pattern"]
    return re.compile(
        rf"\b(?:{it_is})\s+(?:(?:{now})\s+)?{shape}"
        rf"|\b(?:{it_is})\s+(?:{now})\s+{calendar}"
        rf"|\b(?:{today})\s+(?:{shape}|{calendar})",
        re.IGNORECASE)


def _correct_unfetched_clock(text: str, now: datetime.datetime | None = None) -> str:
    if not _CLOCK_CLAIM.search(text or ""):
        return text
    now = now or datetime.datetime.now()
    real = CONFIG["clock"]["real_time"].format(
        weekday=f"{now:%A}", day=now.day, month=f"{now:%B}", hour=now.hour % 12 or 12,
        minute=f"{now:%M}", ampm=f"{now:%p}")
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [s for s in sentences if not _CLOCK_CLAIM.search(s)]
    return " ".join([real] + kept)


def _strip_unfetched_weather(text: str) -> str:
    if not _WEATHER_DATA.search(text or ""):
        return text
    kept_lines = []
    for line in text.splitlines():
        sentences = re.split(r"(?<=[.!?])\s+", line)
        kept = [s for s in sentences if not _WEATHER_LINE.search(s)]
        if kept or not line.strip():
            kept_lines.append(" ".join(kept))
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()
    return out or CONFIG["weather"]["not_checked"]


# The requests below are answered by the code before the model (brain.json
# cues): the briefing and the study plan (planner.py); a study brief for
# one module (study_brief.py -- "prep me for signals" went to the Claude
# hand-off and "brief me on electromagnetism" to the daily briefing; a
# brief for a named module is local work from his own notes, and without a
# module she asks which one); the watchlist (invest.py, 2026-10-30:
# read-only, no model call -- there is no trading tool, and adding/removing
# instruments is the CLI's job for now).


def _study_brief_cue(c: dict) -> re.Pattern:
    """study_brief_patterns, plus study_brief_with_module_patterns each
    needing one of module_names later in the sentence."""
    lookahead = r"(?=[^.?!]*\b(?:" + "|".join(c["module_names"]) + r")\b)"
    return _rx(list(c["study_brief_patterns"]) + [p + lookahead for p in c["study_brief_with_module_patterns"]])


# "Go on, do that one" -- the job is named by the turn before. Live on 28-29
# Oct, after she offered the briefing, "alright tell me" got her skills
# list; after "plan my study modules", "i want you to plan it" got "use the
# list_skills tool". Re-probed 2026-10-30: the first invented "It's
# currently December 2023", the second asked which subjects he studies.
# cues.follow_on_pattern; the job carried over is cues.carry_*. A plain yes
# to a planner offer is cues.plan_yes_pattern.

# His internships, projects and trackers live in the vault. Live, "hows my
# internship tracker" was told there was no information, "the projects im
# doing" got one stale saved fact, and "plan my internship applications"
# went skill-hunting.
# Used after the model answers (the skills-list restore below, and
# conversation._drop_failed): a skills list given to something that wasn't
# a skills question is dropped from the replay. No longer a gate on
# list_skills itself (2026-11-03).
_SKILL_QUESTION = tool_policy.cue("skills_question")


# Greetings and acknowledgements have one right kind of answer, and the model
# kept finding wrong ones: "hey" -> "No response needed." 4/6 traced, "I
# didn't catch that." 3/3 in the eval; "thats good" -> "I didn't catch
# that" 3/3, or the clock. This is the 2026-10-24 conclusion applied at last:
# the low-content turn is answered before the model, not argued with.
# Patterns and replies: brain.json quick_replies.


def _quick_reply(text: str, last_said: str, now: datetime.datetime | None = None) -> str | None:
    """The reply to a greeting or an acknowledgement, or None if it isn't one.
    An "ok" that answers one of her own questions is a real answer -- that
    goes to the model, so "want me to open it?" -> "ok" still opens it."""
    t = (text or "").strip()
    q = CONFIG["quick_replies"]
    if _GREETING.fullmatch(t):
        hour = (now or datetime.datetime.now()).hour
        part = next((p["name"] for p in q["day_parts"] if hour < p["until_hour"]), q["day_parts"][-1]["name"])
        if _DAY_PART.search(t):
            return q["greeting_by_time"].format(part=part, name=_NAME)
        return q["greeting"].format(name=_NAME)
    if _NEVER_MIND.fullmatch(t):
        # "nvm" -- dropping the thing, mid-question or not. The model
        # answered it well and called the clock first every time
        # (2026-11-04, 8/8, with the clock's description already saying
        # not to). One right answer, so it's answered here, like a greeting.
        return q["never_mind"]
    if (last_said or "").rstrip().endswith("?"):
        return None
    if _THANKS.fullmatch(t):
        return q["thanks"]
    if _PLAIN_ACK.fullmatch(t):
        return q["ack"]
    return None


# The jobs Denisse hands to Claude (delegate.py). From ~180 of his own
# non-Denisse prompts to Claude Code and Codex: CV and cover-letter writing,
# internship research, pitch prep, people lookups and coursework solutions.
# An 8B model answers these plausibly and badly. Kept explicit, so the line
# is testable: finding his CV is local, improving it is Claude's.
# cues.delegate_patterns, and never cues.keep_local_patterns.


def _should_delegate(text: str) -> bool:
    return bool(_DELEGATE.search(text or "")) and not _KEEP_LOCAL.search(text or "")


def _is_known_skill(name: str) -> bool:
    """read_skill('plan_study_modules') was the opening move of three of the
    week's dead-end replies. A skill that doesn't exist gets a refusal that
    says to do the task instead. If the list can't be read, don't block."""
    try:
        listing = str(tools.TOOL_FUNCTIONS["list_skills"]())
    except Exception:
        return True
    return name in _skill_names(listing)


def _vault_query(text: str) -> str | None:
    """His internships and projects live in the vault: the search_vault
    query for a request about them (cues.vault_queries), else None."""
    t = (text or "").lower()
    for patterns, query in _VAULT_QUERIES:
        if all(rx.search(t) for rx in patterns):
            return query
    return None


def _known_document_path(path: str, messages: list, outputs: dict) -> bool:
    """Only open a document path supplied by Chuen or observed in a lookup.

    A model guessing 'latest_cv.pdf' must search, not use the app launcher
    to pretend the document was found. No filesystem scan is done here.
    """
    if not isinstance(path, str) or not ntpath.isabs(path):
        return False
    wanted = ntpath.normcase(ntpath.normpath(path.strip().strip('"')))
    sources = [m.get("content", "") for m in messages if m.get("role") == "user"]
    found = outputs.get("find_files", "")
    if found.startswith("Found "):
        sources.append(found)
    pattern = re.compile(re.escape(wanted) + r"(?=$|[\s\"'\)\],;]|\.(?:\s|$))")
    return any(pattern.search(source.replace("/", "\\").lower()) for source in sources if source)


# The file fast paths after the model answers -- the search the code runs
# when she is about to answer a file request without having looked, and
# the open that completes "open my cv" once one match is verified. Their
# cues are tool_policy.json data ("open_document", "file_thing",
# "file_lookup"), kept because every other way that went this week was
# wrong (see the loop below); they decide what the code does for her, never
# whether a call of hers is refused.
_OPEN_DOCUMENT_REQUEST = tool_policy.cue("open_document")
_FILE_THING = tool_policy.cue("file_thing")
_FILE_LOOKUP = tool_policy.cue("file_lookup")


def _single_document_path(result: str) -> str:
    if not re.match(r"^Found\s+1\s+match", result):
        return ""
    match = re.search(r"(?:[A-Za-z]:[\\/]|\\\\)[^\r\n]+", result)
    return match.group(0).strip().strip('"') if match else ""


def _get(obj, key, default=None):
    """Works whether ollama hands back a dict or an attribute-style object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _tool_call_to_dict(call) -> dict:
    """ollama hands back ToolCall objects, not plain dicts -- flatten one to
    a plain dict so it's JSON-serializable for memory_store and safe to
    replay back into ollama.chat()."""
    func_info = _get(call, "function")
    return {
        "function": {
            "name": _get(func_info, "name"),
            "arguments": dict(_get(func_info, "arguments") or {}),
        }
    }


def _sniff_fake_tool_call(content: str) -> dict | None:
    """The small local model occasionally writes a tool call as plain JSON
    text (e.g. '{"name": "get_weather", "parameters": {"location": "..."}}')
    instead of using Ollama's structured tool_calls field -- seen with both
    a real tool name and a hallucinated one, and seen both as the *entire*
    reply and (found live, with open_application) tacked onto the end of
    an otherwise normal sentence ("Let me try to open Spotify for you.
    {"name": "open_application", ...}"). Detect either shape so it can be
    recovered as a real call instead of showing broken JSON to the user --
    finds the first '{' rather than requiring the whole message to be
    JSON, so a real prose prefix doesn't hide it from this check."""
    text = (content or "").strip()
    start = text.find("{")
    if start == -1:
        return None
    candidate = text[start:].strip()
    if not candidate.endswith("}"):
        return None
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return None
    args = data.get("parameters", data.get("arguments", {}))
    if not isinstance(args, dict):
        return None
    return {"name": data["name"], "arguments": args}


def _drop_unexpected_args(func, args: dict) -> dict:
    """Keep only the keyword arguments `func` actually accepts.

    An 8B model passes junk arguments regularly -- observed 2026-09-23, it
    called list_tasks(task='the N20 motor') when list_tasks takes none. That
    raised a TypeError, and rather than reporting the error the model
    *invented* a plausible-looking task list, including an item that has
    never existed. A fabricated answer is far worse than a dropped argument,
    so extras are discarded and the call goes ahead.

    Functions that take **kwargs are left alone.
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return args
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return args
    allowed = {
        n for n, p in params.items()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                      inspect.Parameter.KEYWORD_ONLY)
    }
    return {k: v for k, v in args.items() if k in allowed}


def _required_params(func) -> list:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return []
    return [n for n, p in params.items()
            if p.default is inspect.Parameter.empty
            and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                           inspect.Parameter.KEYWORD_ONLY)]


def _rescue_arg_names(func, args: dict) -> dict:
    """Put a value back when the model got the argument's NAME wrong.

    _drop_unexpected_args exists because junk arguments used to raise a
    TypeError and the model covered the error by inventing a result. But
    dropping is the wrong answer when the name was wrong rather than the
    argument spurious: on 2026-10-26 the model called
    find_files(filename='passport'), the value was discarded, the call
    died with "missing 1 required positional argument: 'query'", and
    Denisse told Chuen the file wasn't on his machine. It was. She had
    simply never searched for it -- twice, because read_file then got the
    same treatment.

    Only the unambiguous case is repaired: exactly one required parameter
    left unfilled and exactly one scalar value dropped. Anything less
    clear-cut is left alone for _run_tool to report honestly.
    """
    kept = _drop_unexpected_args(func, args)
    if kept == args:
        return kept
    dropped = [v for k, v in args.items() if k not in kept]
    missing = [p for p in _required_params(func) if p not in kept]
    if len(missing) == 1 and len(dropped) == 1 and isinstance(
            dropped[0], (str, int, float)):
        kept = dict(kept)
        kept[missing[0]] = dropped[0]
    return kept


# Tools that change something and are almost never wanted twice in one
# turn (tool_policy.json, once_per_turn). Completing a task is the case
# that bit: told "I've ordered the N20 motor, tick it off", the model
# closed that task and then closed an unrelated one as well, three runs
# out of three (2026-09-23). Marking work done that Chuen never mentioned
# is quiet, plausible-looking data loss -- he'd only find out when the
# thing he thought was tracked didn't happen.
_ONCE_PER_TURN = tool_policy.once_per_turn()


def _run_tool(name: str, args: dict, confirm, used: set | None = None) -> str:
    """Run one tool call. What the model is told when the code stops it is
    brain.json tool_errors (the opening words are read by multistep.py and
    learning.json, as its note says)."""
    err = CONFIG["tool_errors"]
    func = tools.TOOL_FUNCTIONS.get(name)
    if func is None:
        return err["unknown_tool"].format(name=name)
    if used is not None and name in _ONCE_PER_TURN:
        if name in used:
            return err["already_ran"].format(name=name)
        used.add(name)
    if confirm is not None and not confirm(name, args):
        return err["declined"].format(name=name)
    call_args = _rescue_arg_names(func, args)
    missing = [p for p in _required_params(func) if p not in call_args]
    if missing:
        # Say what to do about it. The raw TypeError told the model the
        # call had failed but not how to fix it, so it gave up and
        # reported the thing as not found instead of calling again.
        return err["missing_argument"].format(name=name, missing=missing[0])
    try:
        return func(**call_args)
    except Exception as e:
        # Be explicit that this failed and that nothing may be invented to
        # cover it -- the model's instinct on a tool error is to make
        # something up that looks like a plausible result.
        return err["failed"].format(name=name, error=e)


def _study_context() -> str:
    try:
        import study_mode
        return study_mode.context_line()
    except Exception:
        return ""


# The learner model (learner/) is the one record of how he learns, decides
# and takes feedback; Don and Atrium read it too. Read through learner.core
# on every turn (a small JSON file), so a new answer shows up without a
# restart; never written from here. With no store, the turn runs as before
# and this says so once per process.
_LEARNER_WARNED = False


def _learner_context() -> str:
    global _LEARNER_WARNED
    lc = CONFIG["learner"]
    try:
        from learner import core
        if not core.STORE.exists():
            raise FileNotFoundError(str(core.STORE))
        data = core.load()
    except Exception as exc:  # noqa: BLE001 -- no learner model is not a reason not to answer
        if not _LEARNER_WARNED:
            _LEARNER_WARNED = True
            logging.info(lc["missing_log"].format(error=exc))
        return lc["none"]
    order = {a: i for i, a in enumerate(lc["areas"])}
    beliefs = sorted((b for b in data.get("beliefs", [])
                      if b.get("status") == lc["status"] and b.get("area") in order and b.get("claim")),
                     key=lambda b: order[b["area"]])
    lines = [lc["line"].format(claim=b["claim"]) for b in beliefs[:lc["max"]]]
    return lc["block"].format(claims="\n".join(lines)) if lines else lc["none"]


def _own_words(result: str, markers: int | None = None) -> str:
    """A tool's own sentence: its result up to the first of
    honesty.result_cut_markers (the first `markers` of them)."""
    out = result
    for marker in CONFIG["honesty"]["result_cut_markers"][:markers]:
        out = out.split(marker)[0]
    return out.strip()


# The task tools' own words, turned into hers. "Added: remind me there is
# a robotics meeting (today 17:00)" went to him verbatim on 2026-11-03.
# Deterministic on purpose -- the date in it must be the store's, because
# the model once invented "6th of October at 2 PM" -- but a sentence.
def _task_sentence(name: str, result: str) -> str:
    """The store's own format is parsed here; the sentences are brain.json
    tasks."""
    r = (result or "").strip()
    say = CONFIG["tasks"]
    if name == "add_task":
        m = re.match(r"Added:\s*(.+?)\s*(?:--\s*due\s+(.+?)\.?|\((no deadline)\)|--\s*but\s+(.+?))\s*$", r, re.S)
        if not m:
            return r
        text, due, none, trouble = m.groups()
        if due:
            return say["added_due"].format(text=text, due=due.rstrip("."))
        if trouble:
            return say["added_trouble"].format(text=text, trouble=trouble.rstrip("."))
        return say["added_no_deadline"].format(text=text)
    if name == "complete_task":
        m = re.match(r"Marked done:\s*(.+?)\.\s*(?:That was the last one|(\d+) other tasks? still open)", r)
        if not m:
            return r
        left = m.group(2)
        tail = say["last_one"] if left is None else say["still_open"].format(left=left)
        return say["completed"].format(text=m.group(1), tail=tail)
    if name == "remove_task":
        m = re.match(r"Removed:\s*(.+)$", r)
        return say["removed"].format(text=m.group(1).rstrip(".")) if m else r
    return r


def _dates_agree(reply: str, result: str, asked: str) -> bool:
    """Every date word the model's reply uses is in the tool's result or in
    his own request -- so a natural reply can stand in for the store's
    words without smuggling in a date nobody said."""
    said = set(_DUE_WORDS.findall((reply or "").lower()))
    known = set(_DUE_WORDS.findall((result or "").lower())) | set(_DUE_WORDS.findall((asked or "").lower()))
    return said <= known


def _built(c: dict) -> dict:
    """The module values built from brain.json: every compiled pattern, word
    set and fixed reply the functions above use. Patterns are
    case-insensitive except those run on lower-cased text, the path
    patterns and news.strip_prefix_pattern, which match as written."""
    cs = 0  # case-sensitive
    nets, paths, ev, hon, cues, q, fq, lk = (c["reply_nets"], c["paths"], c["evidence"], c["honesty"],
                                             c["cues"], c["quick_replies"], c["file_query"], c["lookup"])
    name = _first_name()
    defs = _tool_definitions(c)
    return {
        "NUM_CTX": _env_int("DENISSE_NUM_CTX", c["limits"]["num_ctx"]),
        "MAX_TOOL_ROUNDS": _env_int("DENISSE_MAX_TOOL_ROUNDS", c["limits"]["max_tool_rounds"]),
        "_NAME": name,
        "_SECOND_PERSON": _second_person_rules(name, c["second_person"]),
        # the prompt and the tools
        "SYSTEM_PROMPT": _system_prompt(c["prompt"]),
        "TOOL_DEFINITIONS": defs,
        "_TOOL_NAMES": {d["function"]["name"] for d in defs if "_" in d["function"]["name"]},
        # reply nets
        "_TOOL_NARRATION": _rx(nets["tool_narration_pattern"]),
        "_TOOL_TALK": _rx(r"\b(?:" + "|".join(nets["tool_talk_patterns"]) + r")\b" if nets["tool_talk_patterns"] else ""),
        "_TOOL_LEAD_IN": _rx(nets["tool_lead_in_pattern"]),
        "_DIDNT_CATCH": _rx(nets["didnt_catch_patterns"]),
        "_WORD": _rx(nets["word_pattern"], cs),
        "_ROLE_PREFIX": _rx(nets["role_prefix_pattern"], cs),
        # weather and clock
        "_PLACE_FOLLOW_UP": _rx(c["weather"]["follow_up_place_pattern"]),
        "_NOT_A_PLACE": _rx(r"^(?:" + "|".join(c["weather"]["not_a_place"]) + ")$" if c["weather"]["not_a_place"] else ""),
        "_WEATHER_DATA": _rx(c["weather"]["data_patterns"]),
        "_WEATHER_LINE": _rx(c["weather"]["line_patterns"]),
        "_CLOCK_CLAIM": _clock_claim(c["clock"]),
        # paths
        "_WIN_PATH": _rx(paths["windows_path_pattern"], cs),
        "_PATH_ASK": paths["ask"],
        "_WANTS_PATH": _rx(paths["wants_path_patterns"]),
        "_PATH_YES": _rx(paths["yes_pattern"]),
        "_SEARCH_MISS": _rx(paths["search_miss_pattern"], cs),
        "_ANY_PATH": _rx(paths["any_path_pattern"], cs),
        # notes and dates
        "_NOTES_HEADER": _rx(c["notes"]["header_pattern"], re.MULTILINE),
        "_ASKS_DUE": _rx(c["notes"]["asks_due_pattern"]),
        "_DUE_YEAR": _rx(c["notes"]["due_year_pattern"]),
        "_YEAR": _rx(c["notes"]["year_pattern"], cs),
        "_DUE_WORDS": _rx(r"\d+|\b(?:" + "|".join(c["dates"]["due_words"]) + ")" if c["dates"]["due_words"] else r"\d+"),
        "_DUE_ALIAS": {a["said"]: a["means"] for a in c["dates"]["due_aliases"]},
        "_DUE_ALWAYS": set(c["dates"]["always_allowed"]),
        # tasks
        "_DEADLINE_ONLY": _rx(c["tasks"]["deadline_only_pattern"]),
        "_TASK_WORD": _rx(c["tasks"]["content_word_pattern"], cs),
        "_TASK_STOP": set(c["tasks"]["content_stop_words"]),
        # news
        "_NEWS_HEAD": _rx(c["news"]["head_pattern"]),
        "_NEWS_STRIP": _rx(c["news"]["strip_prefix_pattern"], cs),
        "_NEWS_WORD": _rx(c["news"]["word_pattern"], cs),
        # honesty
        "UNCONFIRMED_REPLY": hon["unconfirmed_reply"],
        "FAILED_REPLY": hon["failed_reply"],
        "_CLAIM_NEEDS": [(_rx(n["pattern"]), set(n["tools"])) for n in hon["claim_needs"]],
        "_SET_FOR": _rx(hon["set_for_pattern"]),
        "_DELETE_CLAIM": _rx(hon["delete_claim_pattern"]),
        "_NEGATED_BEFORE": _rx(hon["negated_before_pattern"], cs),
        "_DELETE_TOOLS": set(hon["delete_tools"]),
        "_STATE_TOOLS": tuple(hon["state_tools"]),
        # evidence and lookups
        "_NO_EVIDENCE": _rx(ev["no_evidence_pattern"], cs),
        "_FOUND_NOTHING": _rx(r"^(?:" + "|".join(ev["found_nothing_patterns"]) + ")" if ev["found_nothing_patterns"] else ""),
        "_RECOVERY_SOURCES": {s["tool"]: tuple(s["then"]) for s in ev["recovery_sources"]},
        "_LOOKUP_FILLER": set(ev["lookup_filler"]),
        "_LOOKUP_WORD": _rx(ev["word_pattern"], cs),
        "_SAYS_NOTHING": _rx(ev["says_nothing_patterns"]),
        "_LOOKUP_WHERE": {w["tool"]: (w, _rx(w["unreachable_pattern"]) if w["unreachable_pattern"] else None)
                          for w in lk["where"]},
        "_VAULT_ACCESS_DENIAL": _rx(c["vault_denial_patterns"]),
        "_FILE_ABSENCE": _rx(c["file_absence_patterns"]),
        # requests answered before the model
        "_NEWS_OFF": _rx(cues["news_off_pattern"]),
        "_SELF_MODEL": _rx(cues["self_model_patterns"]),
        "_BRIEFING": _rx(cues["briefing_patterns"]),
        "_PLAN_STUDY": _rx(cues["plan_study_patterns"]),
        "_WATCHLIST_BRIEF": _rx(cues["watchlist_brief_pattern"]),
        "_WATCHLIST_SHOW": _rx(cues["watchlist_show_pattern"]),
        "_STUDY_BRIEF": _study_brief_cue(cues),
        "_FOLLOW_ON": _rx(cues["follow_on_pattern"]),
        "_PLAN_YES": _rx(cues["plan_yes_pattern"]),
        "_ACK": _rx(cues["ack_pattern"]),
        "_DELEGATE": _rx(cues["delegate_patterns"]),
        "_KEEP_LOCAL": _rx(cues["keep_local_patterns"]),
        "_DONE_WORD": _rx(cues["done_word_pattern"]),
        "_STYLE_NOTE_EXCLUDES": _rx(cues["style_note_excludes_pattern"]),
        "_VAULT_QUERIES": [([_rx(p, cs) for p in v["all_of_patterns"]], v["query"]) for v in cues["vault_queries"]],
        "_FEEDBACK_COMMANDS": {f["command"].lower(): f["reply"] for f in c["text"]["feedback_commands"]},
        # quick replies
        "_GREETING": _rx(q["greeting_pattern"]),
        "_THANKS": _rx(q["thanks_pattern"]),
        "_PLAIN_ACK": _rx(q["ack_pattern"]),
        "_NEVER_MIND": _rx(q["never_mind_pattern"]),
        "_DAY_PART": _rx(q["day_part_pattern"]),
        "_VERIFY_CLAIM": _rx(cues["verify_claim_pattern"]),
        "_PROMISE": _rx(cues["promise_pattern"]),
        # file query and skills
        "_FILE_PATH_GIVEN": _rx(fq["path_pattern"], cs),
        "_FILE_NAMED": [_rx(p, cs) for p in fq["named_patterns"]],
        "_FILE_WORD": _rx(fq["word_pattern"], cs),
        "_FILE_QUERY_STOP": set(fq["stop_words"]),
        "_GENERIC_SKILL_WORDS": set(c["skills"]["generic_words"]),
        "_SKILL_STRIP": _rx(c["skills"]["strip_prefix_pattern"]),
    }


def configure(path=None) -> dict:
    """(Re)load brain.json (or `path`, or $DENISSE_BRAIN_DATA) and rebind
    the module values built from it. Tests call it with their own file."""
    global CONFIG
    CONFIG = brain_rules.data(path)
    globals().update(_built(CONFIG))
    return CONFIG


configure()


class Brain:
    def __init__(self):
        try:
            local_models = [m.get("model", m.get("name", "")) for m in ollama.list().get("models", [])]
        except Exception as e:
            raise RuntimeError(CONFIG["startup"]["ollama_unreachable"].format(error=e))
        if not any(MODEL in m for m in local_models):
            raise RuntimeError(CONFIG["startup"]["model_missing"].format(model=MODEL))

    def _ask_which(self, request, terms, reads, wrong_reply, confirm, messages):
        """The question to ask after a rejected read, or None to fall back
        to the retry. Only when the rejected answer visibly came from the
        evidence, or his words pick out several items in it."""
        import clarify
        cands, by_terms = clarify.candidates(reads, terms)
        gone = clarify.rejected(cands, wrong_reply)
        if not gone and not (by_terms and len(cands) >= 2):
            return None
        others = [c for c in cands if c not in gone]
        if not others:
            # Nothing else in what was read: one other permitted source for
            # the same subject, as in the empty-lookup repair -- not more.
            tried, offered = {c["name"] for c in reads}, _permitted_reads()
            for alt in dict.fromkeys(a for c in reads for a in _RECOVERY_SOURCES.get(c["name"], ())):
                if alt in tried or alt not in offered or not terms:
                    continue
                args = _lookup_args(alt, terms)
                res = str(_run_tool(alt, args, confirm, set()))
                messages += [{"role": "assistant", "content": "",
                              "tool_calls": [{"function": {"name": alt, "arguments": args}}]},
                             {"role": "tool", "content": res}]
                if not _no_evidence(res):
                    seen = {g["text"] for g in gone}
                    others = [c for c in clarify.candidates([{"name": alt, "arguments": args, "result": res}],
                                                            terms)[0] if c["text"] not in seen]
                    cands = cands + others
                break
        if not others:
            return clarify.missing(terms, gone or cands)
        asked = clarify.question(others)
        clarify.offer(request, asked, others, cands, terms)
        return asked

    def _move_new_task(self, text, last_said):
        """A bare deadline ("before 2nd of november") right after she added
        a task sets that task's deadline. The reply, or None if it isn't one."""
        if not _DEADLINE_ONLY.fullmatch((text or "").strip()):
            return None
        import tasks
        if tasks.parse_due(text) is None:
            return None
        recent = [t for t in tasks.list_tasks() if not t.get("done_at")]
        if not recent:
            return None
        newest = max(recent, key=lambda t: t.get("created") or "")
        try:
            age = datetime.datetime.now() - datetime.datetime.fromisoformat(newest["created"])
        except (KeyError, TypeError, ValueError):
            return None
        rules = CONFIG["tasks"]
        if (age > datetime.timedelta(minutes=rules["move_window_minutes"])
                or newest["text"].lower()[:rules["move_match_chars"]] not in (last_said or "").lower()):
            return None
        updated = tasks.update_task(newest["id"], due=text.strip())
        return rules["moved"].format(task=tasks.describe(updated)) if updated else None

    def _settle_which(self, p, text):
        """His answer to "which one?": the answer from the item he picked,
        another question, or None when it's a new request."""
        import clarify
        kind, got = clarify.pick(text, p, complaint=bool(_COMPLAINT.fullmatch((text or "").strip())))
        if kind == "unrelated":
            return None
        if kind == "none":
            return clarify.missing(p["terms"], p["seen"])
        if kind == "pick":
            evidence = clarify.evidence(got)
            reply = ""
            try:  # one call, with only the item he picked: it can't repeat the rejected one
                r = local_llm.chat(job=LLM_JOB, **_chat_extra(), model=MODEL, keep_alive=CONFIG["chat"]["keep_alive"], options=_chat_options(),
                                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                                          {"role": "user", "content": p["request"]},
                                          {"role": "system", "content": CONFIG["nudges"]["clarified_answer"].format(
                                              spoken=clarify.spoken(got), evidence=evidence)}])
                reply = _strip_role_prefix(_get(_get(r, "message"), "content", "") or "").strip()
            except Exception as exc:
                logging.warning("Clarified answer fell back to the evidence: %s", exc)
            return reply if clarify.supported(reply, got, p["seen"]) else clarify.quote(got)
        options = got if kind == "ambiguous" else p["options"]
        asked = (CONFIG["text"]["not_one_found"] if kind == "unsupported" else "") + clarify.question(options)
        clarify.offer(p["request"], asked, options, p["seen"], p["terms"])
        return asked

    def respond(self, messages: list, confirm=None, extra_context: str = "") -> tuple[str, list]:
        """
        Run one turn of the tool-use loop against `messages` (role/content
        dicts, not including the system prompt). Returns
        (final_text, updated_messages).

        `confirm`, if given, is called as confirm(tool_name, args) -> bool
        before each tool call and can veto it (see safety.py). With no
        confirm callback, every tool call runs unchecked.

        `extra_context`, if given, is appended to the system prompt for
        *this call only* -- it is never added to `messages`, so it's never
        persisted by memory_store.save_history(). Used by wake_word.py's
        short rolling buffer of recently-heard speech (deliberately
        memory-only, never written to disk -- see its own module docs).
        """
        system_content = SYSTEM_PROMPT
        if extra_context:
            system_content += "\n\n" + extra_context
        # Disabled (2026-09-02): injecting the shared Obsidian Dev/Memory/
        # folder here measurably broke reply quality -- tested directly
        # against a real question (news summarization): 0/4 clean even
        # trimmed to 600 chars (one run hallucinated a fake save action),
        # 4/4 clean with this off. obsidian_sync.read_claude_memory()
        # itself is untouched -- re-enable by uncommenting below if the
        # underlying content quality improves.
        # claude_context = obsidian_sync.read_claude_memory()
        # if claude_context:
        #     system_content += (
        #         "\n\nAdditional context Claude Code/Claude Desktop have saved "
        #         "to the shared Obsidian memory vault:\n\n" + claude_context
        #     )
        # His standing instructions on how to reply (reply_preferences.py)
        # and the study clock's live state (study_mode.context_line): both
        # this call only, never in `messages`, so never persisted. The
        # study line is empty when nothing runs, so idle turns pay nothing.
        # How he works, from the learner model: also this call only.
        for block in (reply_preferences.prompt_block(), _learner_context(), _study_context()):
            if block:
                system_content += "\n\n" + block
        full_messages = ([{"role": "system", "content": system_content}]
                         + _style_examples() + messages)
        latest_user = next((m.get("content", "") for m in reversed(messages)
                            if m.get("role") == "user"), "")
        # "what's this?" with nothing else to go on: one line naming the
        # front window, this call only -- never in `messages`, so never
        # saved or synced. See desktop_context.context_line for the rule.
        if not tool_router.feedback_only(latest_user):
            window_line = desktop_context.context_line(latest_user)
            if window_line:
                system_content += "\n\n" + window_line
                full_messages[0] = {"role": "system", "content": system_content}
        if speech_control.is_stop_request(latest_user):
            speech_control.stop()
            # Silence is the requested behaviour. Do not ask a model to
            # manufacture a spoken 'I'll stop talking' acknowledgement.
            messages.append({"role": "assistant", "content": ""})
            return "", messages
        text = CONFIG["text"]
        limits = CONFIG["limits"]
        if _NEWS_OFF.search(latest_user):
            # Act on it rather than describe it -- see cues.news_off_pattern.
            import news
            news.set_briefing_enabled(False)
            answer = text["news_off"]
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        if _SELF_MODEL.search(latest_user):
            answer = _model_identity()
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        last_said = next((m.get("content") or "" for m in reversed(messages[:-1])
                          if m.get("role") == "assistant" and (m.get("content") or "").strip()), "")
        if (last_said.rstrip().endswith(_PATH_ASK) and getattr(self, "_last_paths", None)
                and _PATH_YES.fullmatch(latest_user.strip())):
            answer = text["paths_shown"].join(self._last_paths[:CONFIG["paths"]["max_shown"]])
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        moved = None if getattr(self, "_retrying", False) else self._move_new_task(latest_user, last_said)
        if moved:
            messages.append({"role": "assistant", "content": moved})
            return moved, messages
        if _style_note(latest_user) and not getattr(self, "_retrying", False):
            # His words: "just say 'alright noted'". See _STYLE_NOTE. Saved
            # as a reply preference, so every later prompt carries it.
            saved = _write_style_note(latest_user)
            answer = text["noted"] if not saved.startswith("Error") else text["noted_unsaved"]
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        # An open "which one do you mean?" (clarify.py) is settled first, so
        # no other path -- a quick "yes", a planner confirmation -- answers
        # it, and it answers nothing else. Only her last reply can be it.
        import clarify
        open_q = None if getattr(self, "_retrying", False) else clarify.take(next(
            (m.get("content") or "" for m in reversed(messages[:-1]) if m.get("role") == "assistant"), ""))
        if open_q:
            answer = self._settle_which(open_q, latest_user)
            if answer is not None:
                messages.append({"role": "assistant", "content": answer, "clarify": clarify.is_open(answer)})
                return answer, messages
        if _COMPLAINT.fullmatch(latest_user.strip()) and not getattr(self, "_retrying", False):
            # Redo what he actually asked. The wrong answer and the complaint
            # are left out of the retry, so she can't imitate the miss.
            prev_i = _last_real_request(messages)
            if prev_i is not None:
                prev = (messages[prev_i].get("content") or "").strip()
                wrong_reply = next((m.get("content") or "" for m in reversed(messages[prev_i + 1:-1])
                                    if m.get("role") == "assistant"), "")
                clip, dots = limits["shown_request_chars"], limits["ellipsis"]
                shown = prev if len(prev) <= clip else prev[:clip - len(dots)].rstrip() + dots
                you_asked = text["you_asked"].format(shown=shown)
                attempt = _corrected_attempt(prev, messages, prev_i)
                # What kind of miss it was decides the repair (2026-10-30;
                # live baseline: 3/9 recovered, 0/3 blocked said why, and
                # 0/3 kept from adding the same task twice).
                done = [c for c in attempt if c["name"] in brain_rules.MUTATING_TOOLS
                        and not _confirms_nothing(c.get("result", ""))]
                if done:
                    # An action that already happened. The complaint may be
                    # about the wording or the meaning; redoing it is never
                    # the fix. Say what was done and ask -- no model call.
                    what = "; ".join(str(c.get("result", ""))[:limits["result_preview_chars"]] for c in done)
                    answer = you_asked + text["already_done"].format(what=what)
                    messages.append({"role": "assistant", "content": answer})
                    return answer, messages
                seeded, checked = [], []
                terms = _lookup_terms(prev)
                # The read found something and he rejected the answer: if the
                # evidence holds other plausible answers, ask which one
                # instead of answering again (0/5 live before; clarify.py).
                reads = [c for c in attempt if c["name"] not in brain_rules.MUTATING_TOOLS
                         and not _no_evidence(c.get("result", ""))]
                asked = self._ask_which(prev, terms, reads, wrong_reply, confirm, messages) \
                    if reads and wrong_reply else None
                if asked:
                    # Marked so replay() keeps this turn: his answer refers to it.
                    messages.append({"role": "assistant", "content": asked, "clarify": asked.endswith("?")})
                    return asked, messages

                def irrelevant(c):
                    # A lookup that returned something, none of it about his
                    # subject (list_known_facts -> his coffee order, asked
                    # about his gym), found nothing either.
                    res = str(c.get("result", ""))
                    # Substring, not tokens: "Note 'Sponsor meeting':" must
                    # count as mentioning "meeting" (a token match missed it
                    # and the reply then said no note mentioned it).
                    return _no_evidence(res) or (c["name"] in _RECOVERY_SOURCES and terms != "" and
                                                 not any(t in res.lower() for t in terms.split()))
                if attempt and all(irrelevant(c) for c in attempt) \
                        and any(c["name"] in _RECOVERY_SOURCES for c in attempt):
                    # A lookup that found nothing: check one other permitted
                    # read-only source for the same subject before the model
                    # tries again. Another spelling of the same call is not
                    # a new source, so tools already tried are skipped.
                    # Permitted = defined, read-only and switched on. Not "what
                    # the router offered": the router trims for the model's
                    # attention, and live it hid search_vault from "do i have
                    # anything saved about the dentist".
                    offered = _permitted_reads()
                    tried = {c["name"] for c in attempt}
                    checked = [(c["name"], str(c.get("result", ""))) for c in attempt]
                    for alt in dict.fromkeys(a for c in attempt for a in _RECOVERY_SOURCES.get(c["name"], ())):
                        if alt in tried or alt not in offered or not terms:
                            continue
                        args = _lookup_args(alt, terms)
                        res = str(_run_tool(alt, args, confirm, set()))
                        seeded += [{"role": "assistant", "content": "",
                                    "tool_calls": [{"function": {"name": alt, "arguments": args}}]},
                                   {"role": "tool", "content": res}]
                        checked.append((alt, res))
                        if not _no_evidence(res):
                            break
                    if not seeded or _no_evidence(seeded[-1]["content"]):
                        messages.extend(seeded)
                        answer = you_asked + _blocked_reply(terms or CONFIG["lookup"]["fallback_terms"], checked)
                        messages.append({"role": "assistant", "content": answer})
                        return answer, messages
                # 2026-10-30: the retry got the same input as the miss, so it
                # could make the very call that had just failed. A call
                # that found nothing is refused if repeated unchanged; one
                # that worked may run again (the complaint may be the wording).
                self._retry_avoid = {_call_key(c["name"], c["arguments"])
                                     for c in attempt if _no_evidence(c.get("result", ""))}
                # A read that found something, and he still said no: the facts
                # were there, the reading was wrong. Without them the retry
                # answered "when is the meeting" with the clock (0/5 live).
                # Hand back what was already read -- nothing is run again --
                # and say which answer he rejected.
                reread = []
                reads = [c for c in attempt if c["name"] not in brain_rules.MUTATING_TOOLS
                         and not _no_evidence(c.get("result", ""))]
                if not seeded and reads and wrong_reply:
                    for c in reads:
                        reread += [{"role": "assistant", "content": "",
                                    "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}}]},
                                   {"role": "tool", "content": str(c.get("result", ""))}]
                    reread.append({"role": "system", "content": CONFIG["nudges"]["rejected_answer"].format(
                        wrong=wrong_reply[:limits["rejected_reply_chars"]])})
                self._retrying = True
                try:
                    retry, retry_msgs = self.respond([dict(m) for m in messages[:prev_i + 1]] + seeded + reread,
                                                     confirm=confirm, extra_context=extra_context)
                finally:
                    self._retrying = False
                    self._retry_avoid = set()
                retry_msgs = retry_msgs[:prev_i + 1] + retry_msgs[prev_i + 1 + len(reread):] if reread else retry_msgs
                if seeded and _SAYS_NOTHING.search(retry or ""):
                    # Checked against the evidence: she found it, so "I don't
                    # have anything" is wrong. Quote the source instead.
                    retry = CONFIG["notes"]["say"] + seeded[-1]["content"][:limits["notes_quote_chars"]].strip()
                # Keep the repair's tool calls in this turn. They used to be
                # dropped, so the stored repair looked like a bare claim
                # ("claimed an action with no tool call") and his "thats it"
                # could never confirm a lesson. replay() still sends prose only.
                for m in retry_msgs[prev_i + 1:]:
                    if m.get("role") == "tool" or (m.get("role") == "assistant" and m.get("tool_calls")):
                        messages.append(dict(m))
                if wrong_reply and " ".join(retry.lower().split()) == " ".join(wrong_reply.lower().split()):
                    # Checked against the miss: the same answer again is
                    # not a repair, and saying it twice isn't honest.
                    retry = text["same_answer"].format(retry=retry)
                elif wrong_reply and retry.strip():
                    # A different answer to the same request means the first
                    # one was wrong: own it before correcting it. "omg youre
                    # so dumb" after "15% of 240 is 24" got "36." with no
                    # acknowledgement 6/6 (2026-11-04). Wording: text.corrected.
                    retry = text["corrected"].format(retry=retry.strip())
                answer = (you_asked + retry).strip()
                messages.append({"role": "assistant", "content": answer})
                return answer, messages
        if _VERIFY_CLAIM.match(latest_user.strip()) and not getattr(self, "_retrying", False):
            # "did you open it?" after "Opening it now." is a yes/no about
            # her last action, and the answer is the evidence -- what ran
            # for it and what it returned -- not the model's memory of its
            # own words (2026-11-04: read_file / list_directory and "the file
            # isn't there", 2/2). Certain when something ran; when nothing
            # did, only after a promise or claim of hers. Otherwise the
            # model. cues.verify_claim_pattern / promise_pattern; text.verified_*.
            prev_i = _last_real_request(messages)
            if prev_i is not None:
                prev = (messages[prev_i].get("content") or "").strip()
                said = next((m.get("content") or "" for m in reversed(messages[prev_i + 1:-1])
                             if m.get("role") == "assistant"), "")
                attempt = _corrected_attempt(prev, messages, prev_i)
                acted = [c for c in attempt if c["name"] in brain_rules.MUTATING_TOOLS]
                done = [c for c in acted if not _confirms_nothing(c.get("result", ""))]
                clip = limits["result_preview_chars"]
                answer = None
                if done:
                    answer = text["verified_done"].format(what="; ".join(_own_words(str(c.get("result", "")))[:clip] for c in done))
                elif acted:
                    answer = text["verified_failed"].format(what="; ".join(_own_words(str(c.get("result", "")))[:clip] for c in acted))
                elif not attempt and (_CLAIMS_DONE.search(said) or _PROMISE.search(said)):
                    answer = text["verified_nothing"]
                if answer:
                    messages.append({"role": "assistant", "content": answer})
                    return answer, messages
        import planner
        if _PLAN_YES.fullmatch(latest_user.strip()) and planner.pending():
            last_said = next((m.get("content") or "" for m in reversed(messages[:-1])
                              if m.get("role") == "assistant"), "")
            if CONFIG["cues"]["planner_offer_marker"] in last_said:
                answer = planner.accept()
                messages.append({"role": "assistant", "content": answer})
                return answer, messages
        quick = _quick_reply(latest_user, next((m.get("content") or "" for m in reversed(messages[:-1])
                                                if m.get("role") == "assistant"), ""))
        if quick:
            messages.append({"role": "assistant", "content": quick})
            return quick, messages
        if _FOLLOW_ON.fullmatch(latest_user.strip()):
            # Carry the job over from the turn before -- his request or her
            # offer -- so the checks below see what he means. See _FOLLOW_ON.
            prev_i = _last_real_request(messages)
            prev_user = (messages[prev_i].get("content") or "") if prev_i is not None else ""
            last_said = next((m.get("content") or "" for m in reversed(messages[:-1])
                              if m.get("role") == "assistant"), "")
            if _BRIEFING.search(prev_user) or _BRIEFING.search(last_said):
                latest_user = CONFIG["cues"]["carry_briefing"]
            elif _PLAN_STUDY.search(prev_user) or _PLAN_STUDY.search(last_said):
                latest_user = CONFIG["cues"]["carry_study_plan"]
        import invest
        # Her previous reply goes with it: a pending instrument choice answers
        # only the question she has just asked, so an early reply above
        # ("thanks" -> "Any time.") still closes it.
        said = invest.chat(latest_user.strip(), last_said=next(
            (m.get("content") or "" for m in reversed(messages[:-1]) if m.get("role") == "assistant"), ""))
        if said is not None:
            messages.append({"role": "assistant", "content": said})
            return said, messages
        if _WATCHLIST_SHOW.fullmatch(latest_user.strip()) or _WATCHLIST_BRIEF.fullmatch(latest_user.strip()):
            if _WATCHLIST_SHOW.fullmatch(latest_user.strip()):
                answer = invest.list_text()  # a corrupt store says so, never "empty"
            else:
                answer = invest.brief()
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        import study_brief
        brief_module = study_brief.match_module(latest_user)
        last_said = next((m.get("content") or "" for m in reversed(messages[:-1])
                          if m.get("role") == "assistant"), "")
        # "where's my EM coursework brief?" is a file to find, not a brief
        # to write: the word "brief" sent it into the study-brief job 6/6
        # (2026-11-04). A file lookup (the router's files group, a file
        # thing, a find/open word -- the same test the file fast paths use)
        # goes to the model and its file tools instead.
        own_groups = tool_router._groups_in(latest_user)
        file_lookup_now = ("files" in own_groups and bool(_FILE_THING.search(latest_user))
                           and bool(_OPEN_DOCUMENT_REQUEST.search(latest_user) or _FILE_LOOKUP.search(latest_user)))
        if not file_lookup_now and (
                _STUDY_BRIEF.search(latest_user)
                or (brief_module and _PLAN_STUDY.search(latest_user))
                or (brief_module and last_said.startswith(study_brief.WHICH_MODULE))):
            # One module, done as a job: notes read, brief written into his
            # vault, read back and checked -- see study_brief.py. Before the
            # daily briefing ("brief me on signals") and the hand-off ("prep
            # me for...") so neither swallows it.
            answer = study_brief.run(latest_user)
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        if _BRIEFING.search(latest_user):
            # Built from his tasks and coursework notes -- see planner.py.
            answer = planner.briefing()
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        if _PLAN_STUDY.search(latest_user):
            text, steps = planner.study_plan()
            question = planner.offer(steps)
            answer = text + (f"\n\n{question}" if question else "")
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        if _should_delegate(latest_user) and not getattr(self, "_in_plan", False):
            # A writing/research/solutions job: Claude does it and the result
            # lands in his vault -- see delegate.py. She says so plainly.
            import delegate
            context = [{"role": m["role"], "content": m.get("content") or ""}
                       for m in messages[:-1]
                       if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()]
            answer = delegate.start(latest_user.strip(), context)
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        if latest_user.strip().lower() in _FEEDBACK_COMMANDS:
            answer = _FEEDBACK_COMMANDS[latest_user.strip().lower()]
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        if tool_router.feedback_only(latest_user) and _DONE_WORD.search(latest_user):
            # A complaint about 'Done' isn't permission for another action.
            # Don't turn it into an invented task, fact, path or promise.
            answer = text["done_complaint"]
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        import multistep
        if (not getattr(self, "_in_plan", False) and multistep.enabled()
                and multistep.is_multistep(latest_user)):
            # Two or more separate actions: plan, then one step at a time
            # through this same loop -- see multistep.py. None = not a plan.
            planned = multistep.run(self, messages, confirm, extra_context, latest_user.strip())
            if planned is not None:
                return planned
        # A date in a task has to be his, so the due check reads his request,
        # not a step the model wrote.
        intent_text = getattr(self, "_plan_request", None) or latest_user
        last_tool_result = ""  # fallback for _unleak_json
        used_once = set()      # guards _ONCE_PER_TURN tools; per turn, not per round
        turn_results = []      # every tool result this turn, for the _CLAIMS_DONE check
        successful_tools = set()  # attempts and successful actions are different
        tool_outputs = {}
        blocked_after = set()  # tools a refused not_after call tried to follow (tool_policy.refuse_after)
        news_result = ""       # for _has_news_content
        honesty_nudged = False # that check sends her back at most once per turn
        catch_retried = False  # the "didn't catch that" retry, also once
        # Only the tools this turn could plausibly need -- see tool_router.py
        # for why sending all of them costs reply quality on an 8B model.
        turn_tools = tool_router.select(TOOL_DEFINITIONS, messages)
        plan_tools = getattr(self, "_plan_tools", None)
        if plan_tools is not None:
            # A plan step: multistep.py routed it (with the step before it,
            # for "...from it"), capped at what the whole request routes to.
            turn_tools = [d for d in TOOL_DEFINITIONS if d["function"]["name"] in plan_tools]
        offered_names = {d["function"]["name"] for d in turn_tools}
        request_text, request_groups = tool_router.request_context(messages)
        # The file fast paths below (search for her, complete a verified
        # open) key on the request being about a file. Cues are policy data.
        file_request = "files" in request_groups and bool(_FILE_THING.search(request_text))
        completion_attempted = False
        search_forced = False  # the fallback file search runs at most once a turn
        vault_forced = False   # and so does the fallback vault search
        # A find/open request, as opposed to e.g. "how do I save a document".
        file_lookup_request = file_request and bool(
            _OPEN_DOCUMENT_REQUEST.search(request_text) or _FILE_LOOKUP.search(request_text))

        def execute(name, args):
            # What may run is tool_policy.json, checked on the CALL -- never
            # on his wording (that was the 3 Nov failure; see tool_policy.py).
            # The offered set is a budget for the model's attention, not a
            # permission: a read-only lookup the model decides it needs runs
            # whether or not the router had offered it. A tool that changes
            # something runs only when offered -- which keeps complaints
            # (the router filters those) and hallucinated calls from acting.
            err = CONFIG["tool_errors"]
            if last_round:
                result = err["last_round"]
            elif name not in tools.TOOL_FUNCTIONS:
                result = err["no_such_tool"].format(name=name)
            elif name not in offered_names and not tool_policy.allowed_unoffered(name):
                result = err["not_offered"].format(name=name)
            elif _call_key(name, args) in (getattr(self, "_step_banned", None) or ()):
                result = err["step_repeated"].format(name=name)
            elif _call_key(name, args) in getattr(self, "_retry_avoid", ()):
                # Naming alternatives here was tried live on 2026-10-30: 0/5
                # found the answer (she offered to search instead), ~2x slower.
                result = err["retry_repeated"].format(name=name)
            elif refusal := tool_policy.refuse_after(name, tool_outputs):
                result = refusal
                blocked_after.update(set(tool_policy.tool(name).get("not_after", ())) & set(tool_outputs))
            elif (refusal := tool_policy.refuse(
                    name, args, program_folder=_REPO_DIR,
                    verified_path=lambda p: _known_document_path(p, messages, tool_outputs),
                    known_skill=_is_known_skill)):
                result = refusal
            else:
                if name == "search_vault":
                    import instincts
                    if instincts.wants_vault_overview(latest_user):
                        args = {**args, "query": CONFIG["cues"]["vault_overview_query"]}
                    elif _vault_query(latest_user):
                        # His projects and internships have a known query
                        # (cues.vault_queries). "what am I working on right
                        # now?" -> search_vault('working on right now') hit an
                        # unrelated note and she answered from her own folder
                        # name (2026-11-04, 4/6). Use the known query.
                        args = {**args, "query": _vault_query(latest_user)}
                if name == "add_task" and args.get("due") and not _due_said(str(args["due"]), intent_text):
                    args = {**args, "due": ""}  # see _due_said
                result = str(_run_tool(name, args, confirm, used_once))
                q = str(args.get("query") or args.get("pattern") or "")
                if name == "find_files" and result.startswith("No files matching") and re.search(r"[*?]", q):
                    # "*.cv" found nothing on 2026-10-31; he has hundreds of
                    # files with "cv" in the name. Try the plain word.
                    plain = re.sub(r"[*?]+", " ", q).strip(" .")
                    if plain and plain != q:
                        result = str(_run_tool(name, {"query": plain}, confirm, used_once))
                if (name == "find_files" and result.startswith("No files matching")
                        and "search_vault" in offered_names and "search_vault" not in tool_outputs
                        and tool_router._COMPILED["notes"].search(latest_user)):
                    # 2026-11-03 eval: "where are the CAB tutorial 5 solutions"
                    # -> find_files only -> "couldn't find them, any hint?",
                    # while the Week 5 note says where they are. A module
                    # file that isn't on disk under that name is usually
                    # linked from his notes: check them once, for his words.
                    vres = str(_run_tool("search_vault", {"query": _lookup_terms(latest_user) or latest_user},
                                         confirm, used_once))
                    tool_outputs["search_vault"] = vres
                    turn_results.append(vres)
                    if not _no_evidence(vres):
                        successful_tools.add("search_vault")
                    result += CONFIG["notes"]["appended_to_result"] + vres
                vq = _vault_query(latest_user)
                if (name == "recall" and vq and "search_vault" in offered_names
                        and "search_vault" not in tool_outputs):
                    # "the projects im doing" -> recall('projects') -> one stale
                    # saved fact. His projects are in his notes; read those too.
                    vres = str(_run_tool("search_vault", {"query": vq}, confirm, used_once))
                    tool_outputs["search_vault"] = vres
                    turn_results.append(vres)
                    if not _confirms_nothing(vres):
                        successful_tools.add("search_vault")
                    result += CONFIG["notes"]["appended_to_result"] + vres
            turn_results.append(result)
            tool_outputs[name] = result
            if not _confirms_nothing(result):
                successful_tools.add(name)
            return result

        for _round in range(MAX_TOOL_ROUNDS + 1):
            # Last round: stop offering tools, so the model has to answer in
            # words instead of asking for another call it won't get. See
            # MAX_TOOL_ROUNDS for why this is bounded at all.
            last_round = _round == MAX_TOOL_ROUNDS
            round_tools = None if last_round else turn_tools

            # keep_alive=-1: keep the model loaded in memory indefinitely.
            # Ollama's default unloads it after 5 minutes idle -- measured a
            # cold reload at ~4-6s vs ~0.5s warm, a real hit for something
            # like the wake-word listener that's used sporadically.
            # temperature=0.2: Ollama's own default (~0.8) is high-variance
            # enough that the exact same question could come back as a
            # correct summary of real tool data one run and a complete
            # non-sequitur ("doing alright, you?" to a news question) the
            # next -- measured directly (get_news: inconsistent/often wrong
            # at default temperature, 4/4 clean and on-topic at 0.2, no
            # loss of personality on casual chat).
            response = local_llm.chat(job=LLM_JOB, **_chat_extra(), 
                model=MODEL, messages=full_messages, tools=round_tools, keep_alive=CONFIG["chat"]["keep_alive"],
                options=_chat_options(),
            )
            msg = _get(response, "message")
            tool_calls = _get(msg, "tool_calls") or []

            if not tool_calls:
                raw_text = _strip_role_prefix(_get(msg, "content", "") or "")
                fake_call = _sniff_fake_tool_call(raw_text)
                final_text = raw_text if fake_call else _unleak_json(raw_text, last_tool_result)
                open_result = tool_outputs.get("open_application", "")
                if (fake_call is None and file_request and not completion_attempted and not last_round
                        and _OPEN_DOCUMENT_REQUEST.search(request_text)
                        and (not open_result or open_result.startswith("Refused: no verified document"))):
                    path = _single_document_path(tool_outputs.get("find_files", ""))
                    if path:
                        # He already asked to open it. With one verified
                        # match there is no new choice to ask him to make.
                        # Use the normal executor/confirmation/history path.
                        fake_call = {"name": "open_application", "arguments": {"app_name": path}}
                        completion_attempted = True
                if (fake_call is None and file_lookup_request and not search_forced
                        and not last_round and "find_files" not in tool_outputs
                        and not successful_tools & {"read_file", "open_application"}):
                    # She is about to answer a file request without having
                    # looked. Every way that went this week was wrong: asking
                    # him for the path, "Done." with nothing done, "I didn't
                    # find a file called passport" with no search run, and
                    # two invented PDFs. So the code looks for her, once, and
                    # she answers from what came back.
                    query = _file_query(request_text) or _file_query(latest_user)
                    if query:
                        fake_call = {"name": "find_files", "arguments": {"query": query}}
                        search_forced = True
                vq = _vault_query(latest_user)
                if (fake_call is None and vq and not vault_forced and not last_round
                        and "search_vault" in offered_names and "search_vault" not in tool_outputs):
                    # About his internships or projects, and she's answering
                    # without having read his notes. Same move as the file
                    # search above: look first, then answer from it.
                    fake_call = {"name": "search_vault", "arguments": {"query": vq}}
                    vault_forced = True
                place = _weather_place(latest_user, request_groups)
                if (fake_call is None and place and not last_round
                        and "get_weather" in offered_names and "get_weather" not in tool_outputs):
                    fake_call = {"name": "get_weather", "arguments": {"location": place}}
                if (fake_call is None and not catch_retried and not turn_results
                        and _DIDNT_CATCH.search(final_text) and _clear_message(latest_user)):
                    catch_retried = True
                    # Same question, history left out, no tools: the clean
                    # condition, where this answer was fine. Kept only if
                    # it's an actual answer.
                    retry = local_llm.chat(job=LLM_JOB, **_chat_extra(), 
                        model=MODEL,
                        messages=([full_messages[0]] + _style_examples()
                                  + [{"role": "user", "content": latest_user}]),
                        keep_alive=CONFIG["chat"]["keep_alive"],
                        options=_chat_options(),
                    )
                    retry_text = _strip_role_prefix(
                        (_get(_get(retry, "message"), "content", "") or "").strip())
                    if retry_text and not _DIDNT_CATCH.search(retry_text):
                        final_text = retry_text
                if fake_call is None and _TOOL_NARRATION.match(final_text):
                    # One retry, no tools, with the nudge as a system note
                    # for this call only -- never added to `messages`, so it
                    # is never persisted. Keep the retry if it's usable.
                    retry = local_llm.chat(job=LLM_JOB, **_chat_extra(), 
                        model=MODEL,
                        messages=full_messages + [{"role": "system",
                                                   "content": CONFIG["reply_nets"]["plain_reply_nudge"]}],
                        keep_alive=CONFIG["chat"]["keep_alive"],
                        options=_chat_options(),
                    )
                    # The retry, ending on a system note, comes back as
                    # "assistant\n\nNo worries." -- 3/3 traced 2026-10-21.
                    retry_text = _strip_role_prefix(
                        (_get(_get(retry, "message"), "content", "") or "").strip())
                    if retry_text and not _TOOL_NARRATION.match(retry_text):
                        final_text = retry_text
                elif fake_call is None:
                    final_text = _strip_tool_talk(final_text)
                if news_result and fake_call is None and not _has_news_content(final_text, news_result):
                    final_text = _speak_headlines(news_result) or final_text
                looked = (_looked_text(tool_outputs.get("look_at_this", ""))
                          if "look_at_this" in successful_tools else "")
                if fake_call is None and looked and not _uses_looked_text(final_text, looked):
                    # She read the selection and then only described it --
                    # "It looks like you copied some text about
                    # photosynthesis", every night 3-9 Nov. One retry, no
                    # tools, nudge in this call only; kept only if it works
                    # on the text this time (brain.json looked_text).
                    retry = local_llm.chat(job=LLM_JOB, **_chat_extra(),
                        model=MODEL,
                        messages=full_messages + [{"role": "system", "content":
                                                   CONFIG["looked_text"]["nudge"].format(text=looked)}],
                        keep_alive=CONFIG["chat"]["keep_alive"],
                        options=_chat_options(),
                    )
                    retry_text = _strip_role_prefix(
                        (_get(_get(retry, "message"), "content", "") or "").strip())
                    if retry_text and _uses_looked_text(retry_text, looked):
                        final_text = retry_text
                # A clock lookup or calculation is not evidence that an
                # action happened. A failed timer call isn't evidence either.
                unconfirmed = not (successful_tools & tool_router.MUTATING_TOOLS)
                wrong_tool = _claim_without_its_tool(final_text, successful_tools)
                if fake_call is None and ((unconfirmed and _CLAIMS_DONE.search(final_text)) or wrong_tool):
                    if not honesty_nudged and not last_round:
                        # Back round the loop, tools still on offer, with the
                        # nudge in this call only -- never in `messages`, so
                        # the false claim and the nudge are never persisted.
                        honesty_nudged = True
                        honesty = CONFIG["honesty"]
                        if not turn_results:
                            nudge = honesty["no_tool_claim_nudge"]
                        elif wrong_tool and not unconfirmed:
                            nudge = honesty["wrong_tool_claim_nudge"].format(ran=", ".join(sorted(successful_tools)))
                        else:
                            nudge = honesty["unconfirmed_claim_nudge"].format(
                                results="; ".join(r[:limits["result_preview_chars"]] for r in turn_results))
                        full_messages.append({"role": "system", "content": nudge})
                        continue
                    if turn_results and all(_confirms_nothing(r) for r in turn_results):
                        # Still claiming success over nothing but errors: say
                        # what the tool said, the one thing here that's true.
                        final_text = FAILED_REPLY
                    else:
                        # The retry is best effort, never a licence to let
                        # the same unsupported 'Done' escape a second time.
                        final_text = UNCONFIRMED_REPLY
                if fake_call is None:
                    # Checked after an open too. It used to be skipped once
                    # open_application had succeeded, so she could open the
                    # real file and then report a different, invented one
                    # ("Opened C:\MadeUp\Passport.pdf") -- caught by
                    # test_pet_chat on 2026-10-29. What was actually opened
                    # is in the tool result, so say that instead.
                    if file_request and "read_file" not in successful_tools:
                        said_paths = _ANY_PATH.findall(final_text)
                        if any(not _known_document_path(p.strip().rstrip('"\'.,;`'), messages, tool_outputs)
                               for p in said_paths):
                            lookup = tool_outputs.get("find_files", "")
                            if "open_application" in successful_tools:
                                final_text = tool_outputs["open_application"]
                            else:
                                final_text = (lookup if lookup.startswith(("Found ", "No files matching")) else
                                              text["no_verified_results"])
                    if file_request and _FILE_ABSENCE.search(final_text):
                        lookup = tool_outputs.get("find_files", "")
                        if lookup.startswith("Found "):
                            final_text = lookup
                        elif lookup.startswith("No files matching"):
                            final_text = lookup + text["only_searched_folders"]
                        else:
                            final_text = text["lookup_incomplete"]
                    lookup = tool_outputs.get("find_files", "")
                    if (file_request and lookup.startswith("Found ")
                            and not successful_tools & {"read_file", "open_application"}):
                        paths = _ANY_PATH.findall(lookup)
                        said = final_text.replace("/", "\\").lower()
                        if paths and not any(p.replace("/", "\\").lower() in said for p in paths):
                            final_text += text["matching_files"] + "\n".join(paths[:text["matching_files_shown"]])
                    skills_result = tool_outputs.get("list_skills", "")
                    # Only when he asked about skills. Without this check, on
                    # 2026-10-29 "plan my internship applications" -> she
                    # looked at her skills -> the reply was replaced with the
                    # skills list (a regression from 9287f33).
                    if ("list_skills" in successful_tools and _SKILL_QUESTION.search(latest_user)
                            and not _names_a_skill(final_text, skills_result)):
                        final_text = _skills_reply(skills_result)
                    # A study-mode or preference result is the one true account
                    # of what happened; a reply that contradicts it ("Sutuy
                    # timer paused" over "nothing was paused") is replaced.
                    for name in _STATE_TOOLS:
                        res = tool_outputs.get(name, "")
                        if res and _confirms_nothing(res) and (
                                _CLAIMS_DONE.search(final_text)
                                or _claim_without_its_tool(final_text, successful_tools)
                                or final_text in (FAILED_REPLY, UNCONFIRMED_REPLY)):
                            final_text = _own_words(res)
                    # ...and when it did happen, the reply has to say so. The
                    # eval (2026-11-03, 3 rounds) had pause_study_session run
                    # and the reply be "You've got 25 minutes left in this
                    # block", stop_study_session and "Time to take a break",
                    # a not-running status and "Want to start one?". What a
                    # reply must mention after each tool is policy data
                    # (reply_must_mention); the tool's own sentence stands in
                    # when it doesn't -- alone for a change, in front of her
                    # reply for a plain status.
                    # ...and when the tool ran but did nothing ("No study
                    # session was running, so nothing was stopped"), the
                    # reply has to say that too: "session over" with nothing
                    # running got "See you next time, Chuen." 6/6 on
                    # 2026-11-04 -- no claim for the honesty net to catch,
                    # just the truth left unsaid. nothing_must_mention is the
                    # policy's pattern for that; the tool's own words stand in.
                    for name, res in tool_outputs.items():
                        meta = tool_policy.tool(name)
                        if not res:
                            continue
                        if name in successful_tools:
                            rule, own = meta.get("reply_must_mention"), meta.get("fallback_reply") or _own_words(res, 1)
                        elif _confirms_nothing(res):
                            rule, own = meta.get("nothing_must_mention"), _own_words(res)
                        else:
                            continue
                        # A tool the model tried to follow with a refused
                        # not_after call (marking the quiz it had just set)
                        # gets no say in the reply: 9 Nov, "Thanks for the
                        # practice quiz, Don." named the tutor and stood, and
                        # no question ever reached him.
                        if name not in blocked_after and (not rule or re.search(rule, final_text, re.IGNORECASE)):
                            continue
                        if (tool_policy.side_effect(name) == "read" and final_text.strip()
                                and name in successful_tools and not meta.get("must_mention_replaces")):
                            final_text = f"{own} {final_text.strip()}"
                        else:
                            final_text = own
                    task_result = tool_outputs.get("list_tasks", "")
                    if ("list_tasks" in successful_tools
                            and not successful_tools & {"add_task", "complete_task", "remove_task"}
                            and not _has_task_content(final_text, task_result)):
                        final_text = task_result
                    for name in ("add_task", "complete_task", "remove_task"):
                        result = tool_outputs.get(name, "")
                        confirmed = re.split(
                            r"\.\s+(?:That was the last one|\d+ other tasks? still open)", result, maxsplit=1)[0]
                        # A new task is confirmed in the store's own words
                        # unless her reply names it AND every date in it is
                        # one the store or he said: "I'll remind you on the
                        # 6th of October at 2 PM" named the task right and
                        # invented the date. The store's words are then put
                        # as a sentence, not echoed ("Added: ..." went to him
                        # verbatim on 2026-11-03).
                        # ...and, when the store resolved a deadline, says
                        # WHEN: "I've added the robotics meeting to your
                        # reminders" dropped the 5pm he gave (2026-11-04).
                        says_when = name != "add_task" or "-- due" not in result or bool(_DUE_WORDS.search(final_text.lower()))
                        if (name in successful_tools and result.startswith(("Added:", "Done:", "Marked done:", "Removed:"))
                                and not (_has_task_content(final_text, "- " + confirmed.partition(":")[2])
                                         and (name != "add_task" or _dates_agree(final_text, result, intent_text))
                                         and says_when
                                         and not _talks_about_tools(final_text)
                                         and not final_text.strip().startswith(("Added:", "Marked done:", "Removed:")))):
                            final_text = _task_sentence(name, confirmed if name != "add_task" else result)
                    vault_result = tool_outputs.get("search_vault", "")
                    if vault_result.startswith("From your notes (dated context"):
                        # Preserve dates and recorded-status caveats as well
                        # as topics; keyword overlap cannot verify a summary.
                        final_text = vault_result
                    if (vault_result.startswith("From your notes")
                            and _VAULT_ACCESS_DENIAL.search(final_text)):
                        excerpt = vault_result.partition("\n\n")[2]
                        final_text = CONFIG["notes"]["found"] + excerpt[:limits["vault_excerpt_chars"]]
                    if vault_result.startswith("From your notes") and _ASKS_DUE.search(latest_user):
                        # Preserve an unambiguous due-year from the retrieved
                        # evidence, not the note's unrelated update date.
                        years = set(_DUE_YEAR.findall(vault_result))
                        if len(years) == 1 and not years & set(_YEAR.findall(final_text)):
                            final_text += CONFIG["notes"]["specify_year"].format(year=next(iter(years)))
                    if "get_brain_location" in successful_tools:
                        # Current settings are inspectable facts, not a
                        # persona for the model to invent or paraphrase away.
                        final_text = tool_outputs["get_brain_location"]
                    if "get_weather" not in successful_tools:
                        final_text = _strip_unfetched_weather(final_text)
                    if "get_current_time" not in successful_tools:
                        final_text = _correct_unfetched_clock(final_text)
                    # Last, after every check above has compared paths.
                    final_text, hidden = _tidy_reply(final_text, latest_user)
                    if hidden:
                        self._last_paths = hidden
                    messages.append({"role": "assistant", "content": final_text})
                    return final_text, messages

                # A recovered JSON call, or completion of an explicit
                # single-match file-open request. Record the real call and
                # result, then let the model answer from that evidence.
                assistant_msg = {"role": "assistant", "content": "", "tool_calls": [{"function": fake_call}]}
                messages.append(assistant_msg)
                full_messages.append(assistant_msg)
                result = execute(fake_call["name"], fake_call["arguments"])
                last_tool_result = str(result)
                if fake_call["name"] == "get_news":
                    news_result = last_tool_result
                tool_msg = {"role": "tool", "content": str(result)}
                messages.append(tool_msg)
                full_messages.append(tool_msg)
                continue

            # Model wants to use one or more tools. Keep the tool_calls on the
            # stored assistant message -- without it, the "tool" messages that
            # follow have nothing to answer when this history is replayed
            # (e.g. after a restart), which confuses the model.
            assistant_msg = {
                "role": "assistant",
                "content": _get(msg, "content", "") or "",
                "tool_calls": [_tool_call_to_dict(c) for c in tool_calls],
            }
            messages.append(assistant_msg)
            full_messages.append(assistant_msg)

            for call in tool_calls:
                func_info = _get(call, "function")
                name = _get(func_info, "name")
                args = dict(_get(func_info, "arguments") or {})
                result = execute(name, args)
                last_tool_result = str(result)
                if name == "get_news":
                    news_result = last_tool_result
                tool_msg = {"role": "tool", "content": str(result)}
                messages.append(tool_msg)
                full_messages.append(tool_msg)
            # loop again so the model can use the tool results

        # Only reachable if the final round still produced nothing usable.
        # Say so plainly rather than returning an empty string, which the
        # popup would render as a blank bubble.
        give_up = CONFIG["text"]["gave_up"]
        # If a search already came back, that is a real answer: show it
        # rather than asking him to rephrase a request that was fine. On
        # 2026-10-29 "pull up my passport" ran out of rounds 3/3 in the
        # clean eval while holding all six matches.
        lookup = tool_outputs.get("find_files", "")
        if lookup.startswith(("Found ", "No files matching")):
            give_up = lookup
        give_up, hidden = _tidy_reply(give_up, latest_user)
        if hidden:
            self._last_paths = hidden
        messages.append({"role": "assistant", "content": give_up})
        return give_up, messages
