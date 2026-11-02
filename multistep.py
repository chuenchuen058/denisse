"""Denisse -- multi-step requests, one step at a time.

The roadmap's "honest gap" (2026-11-02): "find my signals coursework, make
a revision list from it and remind me on Thursday" goes to the model as
one turn, with every tool the whole sentence routes to, and an 8B model
does one of the three things and then says it did all of them.

So, only when a request plainly asks for two or more separate actions:

  1. detect      in code, no model call: the request splits on "and" /
                 "then" / commas into at least two parts that each start
                 with an action verb, and some part has a tool cue
                 (tool_router's groups). Unit-tested in
                 scripts/test_planner_multistep.py.
  2. plan        ONE model call, JSON only: {"steps": ["...", ...]}, 2-5
                 short steps. Anything else falls back to the normal turn.
  3. execute     each step goes through the ordinary Brain.respond tool
                 loop, as its own user message, so the router picks tools
                 for that step alone -- and never more than the whole
                 request would have been offered. Earlier steps reach it
                 only as a short summary in that call's system context.
  4. retry once  a step whose tool errored, or whose done-claim the honesty
                 net rejected, gets exactly one more try: the error shown,
                 that exact call refused. A second failure stops the plan
                 and says plainly what failed and what didn't get done.

Nothing here grows SYSTEM_PROMPT: the plan instruction and the per-step
note exist only for the call that uses them, and are never persisted.

DENISSE_PLANNER=0 turns this off -- the control arm of the A/B in
docs/multistep_planning.md.
"""
import json
import logging
import os
import re

MAX_STEPS = 5
SUMMARY_CHARS = 240   # per earlier step, in the next step's context

# Leading words that don't change what is being asked.
_LEAD = re.compile(
    r"^(?:(?:hey|hi|ok(?:ay)?|so|right|also|then|and|please|pls|plz|denisse|now|next"
    r"|after(?:\s+that|wards)?|finally|first(?:ly)?|can\s+you|could\s+you|would\s+you|will\s+you"
    r"|i\s+(?:want|need)\s+you\s+to|i'?d\s+like\s+you\s+to|go\s+and|just)[\s,]+)+",
    re.IGNORECASE)

# Where one action ends and the next begins.
_SPLIT = re.compile(
    r"\s*(?:[,;]\s*(?:and\s+)?(?:then\s+)?|\.\s+|\b(?:and\s+then|and\s+also|and\s+after\s+that"
    r"|after\s+that|then|and|plus)\b\s*)",
    re.IGNORECASE)

# Imperative verbs Denisse could act on. A part only counts as an action
# when it starts with one of these AND says what to act on (two words or
# more): "find and open my cv" is one job, not two.
_VERBS = {
    "find", "locate", "search", "look", "make", "create", "write", "draft", "remind",
    "add", "set", "start", "move", "put", "sort", "organise", "organize", "tidy", "tell",
    "list", "show", "check", "open", "read", "play", "pause", "skip", "give", "save",
    "summarise", "summarize", "plan", "schedule", "note", "tick", "mark", "complete",
    "delete", "remove", "cancel", "copy", "rename", "get", "pull", "bring", "work",
    "compare", "count", "clean", "turn", "stop", "resume", "remember", "email", "send",
    "let", "jot", "book", "figure",
}

_PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["steps"],
    "properties": {"steps": {"type": "array", "minItems": 1, "maxItems": MAX_STEPS,
                             "items": {"type": "string", "maxLength": 200}}},
}

# No example wording in here. Live on 2026-11-02, an example step ("Find my
# signals coursework notes") was planned for a weather request, and quoting
# "remind me"/"tell me" gave "Remind me to find my Japan flight PDF". The
# code's own split of his sentence goes in the user message instead, so
# the model's job is small: make each part stand alone.
_PLAN_PROMPT = (
    "Chuen asked for several things at once. Write the plan: one step for each "
    "thing he asked for, in his order, 2 to 5 steps. Each step must make sense "
    "on its own: keep his words, and replace words like \"it\" or \"them\" with "
    "what they refer to. Only what he asked for. Don't answer anything. Reply "
    "with JSON only: {\"steps\": [\"...\", \"...\"]}"
)
_STOP = {"this", "that", "them", "then", "with", "from", "into", "what", "have", "about", "your", "please"}


def enabled() -> bool:
    return os.environ.get("DENISSE_PLANNER", "1").lower() not in ("0", "false", "off", "no")


def _parts(text: str) -> list:
    return [p for p in (_LEAD.sub("", s.strip(" .!?")) for s in _SPLIT.split(text or "")) if p]


def action_parts(text: str) -> list:
    """The parts of `text` that read as separate instructions."""
    out = []
    for part in _parts(text):
        words = re.findall(r"[a-z']+", part.lower())
        if len(words) >= 2 and words[0] in _VERBS:
            out.append(part)
    return out


def is_multistep(text: str) -> bool:
    """Cheap and conservative: two or more actions, and at least one of them
    needs a tool. A false negative is today's behaviour; a false positive
    costs a model call and splits one job in two."""
    text = (text or "").strip()
    if not text or len(text) > 400 or "\n" in text:
        return False
    if len(action_parts(text)) < 2:
        return False
    import tool_router
    return bool(tool_router._groups_in(text))


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _STOP}


def _his_words(step: str, request: str) -> bool:
    """Mostly his words. Live, "find my japan flight pdf and then add a
    task..." was planned as "Open the computer and navigate to the folder
    where the Japan flight documents are stored" -- 2 of 8 words his."""
    own = _words(step)
    return bool(own) and len(own & _words(request)) * 5 >= len(own) * 3


def parse_plan(raw, request: str | None = None) -> list | None:
    """The steps, or None for anything that isn't a 2-5 step plan. With
    `request`, there must be one step per part the detector found, each
    mostly in his words: anything else is invented, and the plan dropped."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list) or not 2 <= len(steps) <= MAX_STEPS:
        return None
    if any(not isinstance(s, str) or not s.strip() or len(s) > 200 for s in steps):
        return None
    steps = [" ".join(s.split()).rstrip(" .") for s in steps]
    if request is not None and (len(steps) != len(action_parts(request))
                                or not all(_his_words(s, request) for s in steps)):
        return None
    return steps


def make_plan(request: str, chat=None) -> list | None:
    """One JSON-constrained model call. A plan that fails parse_plan is
    replaced by the detector's own parts -- his words, split where he split
    them; a later step's "it" is resolved by step_tools and the summaries.
    None (answer as one turn) only if the model can't be reached or there
    are not 2-5 parts. Never raises."""
    import brain
    chat = chat or brain.ollama.chat
    parts = action_parts(request)
    numbered = "\n".join(f"{i}. {p}" for i, p in enumerate(parts, 1))
    try:
        r = chat(model=brain.MODEL, keep_alive=-1, format=_PLAN_SCHEMA,
                 options={"temperature": 0, "num_ctx": brain.NUM_CTX, "num_predict": 256},
                 messages=[{"role": "system", "content": _PLAN_PROMPT},
                           {"role": "user", "content": f"His request: {request}\nThe parts:\n{numbered}"}])
        raw = brain._get(brain._get(r, "message"), "content", "") or ""
    except Exception as exc:
        logging.warning("Multi-step plan failed, answering as one turn: %s", exc)
        return None
    steps = parse_plan(raw, request)
    if steps is None and 2 <= len(parts) <= MAX_STEPS:
        logging.info("Model plan rejected (%r); using the request's own parts", raw[:200])
        steps = parts
    return steps


def _calls(new_messages: list) -> list:
    """[{name, arguments, result}] in order, from one step's messages."""
    out, pending = [], []
    for m in new_messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            pending = [c.get("function", {}) for c in m["tool_calls"]]
        elif m.get("role") == "tool":
            f = pending.pop(0) if pending else {}
            out.append({"name": f.get("name", "?"), "arguments": f.get("arguments") or {},
                        "result": str(m.get("content") or "")})
    return out


BANNED_PREFIX = "Refused: this exact '"


def _tool_error(result: str) -> bool:
    """The tool itself failed, or he said no. Not a policy refusal ("isn't
    available", "you don't move files"): the model was told why and answers
    from that, which is a reply, not a failure. The first live run counted
    those, and stopped a plan because a no-tool step reached for a tool."""
    import learning
    r = (result or "").strip()
    return (r.startswith("User declined") or r.startswith(BANNED_PREFIX)
            or (learning.tool_failed(r) and not r.startswith("Refused")))


def failure(reply: str, calls: list) -> str | None:
    """The raw result that made a step fail, or None: a tool error that
    nothing later in the step made good, or the honesty net replacing a
    done-claim."""
    import brain
    if (reply or "").strip() in (brain.UNCONFIRMED_REPLY, brain.FAILED_REPLY):
        bad = [c for c in calls if brain._confirms_nothing(c["result"])]
        return bad[-1]["result"] if bad else "nothing actually ran, so it isn't done"
    if calls and _tool_error(calls[-1]["result"]):
        return calls[-1]["result"] or "the tool returned nothing"
    return None


_FOR_THE_MODEL = re.compile(
    r"\s*\b(?:Tell (?:the user|him)|Do not|Don'?t|Call '|Answer the|Use )\b.*$", re.I | re.S)


def plain(result: str) -> str:
    """A tool result as something to say to Chuen: no instructions meant
    for the model, no tool names."""
    import brain
    r = " ".join((result or "").split())
    if r.startswith("User declined"):
        return "you said no to it"
    if r.startswith(BANNED_PREFIX):
        return "the second try repeated the call that had already failed"
    r = re.sub(r"^Error running '[^']+':\s*|^Error:\s*|^Refused:\s*", "", r)
    r = _FOR_THE_MODEL.sub("", r)
    for name in sorted(brain._TOOL_NAMES, key=len, reverse=True):
        r = r.replace(f"'{name}'", "that").replace(name, "that")
    return r.strip(" .")[:200] or "the tool failed"


def _summary(i: int, step: str, reply: str, calls: list) -> str:
    import brain
    said = " ".join((reply or "").split())[:SUMMARY_CHARS]
    good = [c for c in calls if not brain._confirms_nothing(c["result"])]
    got = (" | Found: " + " ".join(good[-1]["result"].split())[:SUMMARY_CHARS]) if good else ""
    return f"{i}. {step} -> {said}{got}"


def _step_note(i: int, n: int, request: str, done: list, error: str | None) -> str:
    note = (f"He asked: \"{request}\". This is step {i} of {n}; the other steps are "
            "handled separately, so do only this one now and answer it in a sentence or two.")
    if done:
        note += "\nEarlier steps (already done -- don't redo them):\n" + "\n".join(done)
    if error:
        note += (f"\nYour last try at this step failed: {error}\nThat exact call is blocked now. "
                 "Do it a different way, or tell him plainly that you couldn't.")
    return note


def _compose(replies: list) -> str:
    """The step replies as one answer. brain's "Want the full path?" offer
    only works as the last thing said, so it moves to the end, once."""
    import brain
    ask, body = False, []
    for r in replies:
        r = (r or "").strip()
        if r.endswith(brain._PATH_ASK):
            ask, r = True, r[:-len(brain._PATH_ASK)].rstrip()
        if r:
            body.append(r)
    return "\n\n".join(body) + (" " + brain._PATH_ASK if ask else "")


def step_tools(step: str, previous: str | None, ceiling: set) -> set:
    """The tools one step is offered: routed on the step itself, through
    tool_router's follow-up rule, so "make a revision list from it" gets
    the tools of the step it refers to -- live, without this, it could not
    read the file the step before had just found. Never more than `ceiling`."""
    import brain
    import tool_router
    msgs = ([{"role": "user", "content": previous}] if previous else []) + [{"role": "user", "content": step}]
    return {d["function"]["name"] for d in tool_router.select(brain.TOOL_DEFINITIONS, msgs)} & ceiling


def run(bot, messages: list, confirm, extra_context: str, request: str):
    """(answer, messages) for a planned multi-step turn, or None to let the
    ordinary single turn handle it."""
    import brain
    import tool_router
    steps = make_plan(request)
    if not steps:
        return None
    # The most any step may be offered: what the whole request routes to.
    ceiling = {d["function"]["name"] for d in tool_router.select(brain.TOOL_DEFINITIONS, messages)}
    done, replies, made = [], [], set()   # made: mutating calls that worked
    saved = {k: getattr(bot, k, None) for k in ("_in_plan", "_plan_tools", "_plan_request", "_step_banned")}
    bot._in_plan, bot._plan_request = True, request
    try:
        for i, step in enumerate(steps, 1):
            bot._plan_tools = step_tools(step, steps[i - 2] if i > 1 else None, ceiling)
            error, shown, banned = None, None, set(made)
            for attempt in (1, 2):
                bot._step_banned = banned
                extra = "\n\n".join(x for x in (extra_context, _step_note(i, len(steps), request, done, shown)) if x)
                reply, out = bot.respond([{"role": "user", "content": step}], confirm=confirm, extra_context=extra)
                new = out[1:]
                messages.extend(dict(m) for m in new
                                if m.get("role") == "tool" or (m.get("role") == "assistant" and m.get("tool_calls")))
                calls = _calls(new)
                for c in calls:
                    if c["name"] in brain.brain_rules.MUTATING_TOOLS and not brain._confirms_nothing(c["result"]):
                        made.add(brain._call_key(c["name"], c["arguments"]))
                why = failure(reply, calls)
                if why is None:
                    error = None
                    break
                # A retry that only hit the ban says nothing new: keep the
                # first reason, the one worth telling him.
                if not (error and why.startswith(BANNED_PREFIX)):
                    error, shown = plain(why), " ".join(why.split())[:200]
                if attempt == 1 and any(c["result"].startswith("User declined") for c in calls):
                    break  # he said no; trying again isn't a different approach
                banned = set(made) | {brain._call_key(c["name"], c["arguments"])
                                      for c in calls if brain._confirms_nothing(c["result"])}
            if error:
                answer = f"I stopped at step {i} ({step}): it didn't work -- {error.rstrip('.')}."
                if replies:
                    answer = _compose(replies + [answer])
                left = steps[i:]
                if left:
                    answer += " Not done: " + "; ".join(left) + "."
                messages.append({"role": "assistant", "content": answer})
                return answer, messages
            replies.append(reply.strip())
            done.append(_summary(i, step, reply, calls))
    finally:
        for k, v in saved.items():
            setattr(bot, k, v)
    answer = _compose(replies) or "I went through each step but have nothing to report."
    messages.append({"role": "assistant", "content": answer})
    return answer, messages
