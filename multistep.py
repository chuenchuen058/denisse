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

_PLAN_PROMPT = (
    "Split Chuen's request into the separate actions it asks for, in the order "
    "he said them. One short instruction per step, in his own words, as if he "
    "had asked for that step alone (\"Find my signals coursework notes\", "
    "\"Remind me on Thursday to revise signals\"). At most 5 steps. Don't add "
    "steps he didn't ask for and don't answer anything yet. Reply with JSON "
    "only: {\"steps\": [\"...\", \"...\"]}"
)


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


def parse_plan(raw) -> list | None:
    """The steps, or None for anything that isn't a 2-5 step plan."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list) or not 2 <= len(steps) <= MAX_STEPS:
        return None
    if any(not isinstance(s, str) or not s.strip() or len(s) > 200 for s in steps):
        return None
    return [" ".join(s.split()) for s in steps]


def make_plan(request: str, chat=None) -> list | None:
    """One JSON-constrained model call. Never raises."""
    import brain
    chat = chat or brain.ollama.chat
    try:
        r = chat(model=brain.MODEL, keep_alive=-1, format=_PLAN_SCHEMA,
                 options={"temperature": 0, "num_ctx": brain.NUM_CTX, "num_predict": 256},
                 messages=[{"role": "system", "content": _PLAN_PROMPT},
                           {"role": "user", "content": request}])
        return parse_plan(brain._get(brain._get(r, "message"), "content", "") or "")
    except Exception as exc:
        logging.warning("Multi-step plan failed, answering as one turn: %s", exc)
        return None


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


def failure(reply: str, calls: list) -> str | None:
    """Why a step failed, or None. A tool error/refusal that nothing after
    it in the step made good, or the honesty net replacing a done-claim."""
    import brain
    if (reply or "").strip() in (brain.UNCONFIRMED_REPLY, brain.FAILED_REPLY):
        bad = [c for c in calls if brain._confirms_nothing(c["result"])]
        return (bad[-1]["result"] if bad else "nothing actually ran, so it isn't done")[:200]
    if calls and brain._confirms_nothing(calls[-1]["result"]):
        return calls[-1]["result"][:200] or "the tool returned nothing"
    return None


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
    bot._in_plan, bot._plan_tools, bot._plan_request = True, ceiling, request
    try:
        for i, step in enumerate(steps, 1):
            error, banned = None, set(made)
            for attempt in (1, 2):
                bot._step_banned = banned
                extra = "\n\n".join(x for x in (extra_context, _step_note(i, len(steps), request, done, error)) if x)
                reply, out = bot.respond([{"role": "user", "content": step}], confirm=confirm, extra_context=extra)
                new = out[1:]
                messages.extend(dict(m) for m in new
                                if m.get("role") == "tool" or (m.get("role") == "assistant" and m.get("tool_calls")))
                calls = _calls(new)
                for c in calls:
                    if c["name"] in brain.brain_rules.MUTATING_TOOLS and not brain._confirms_nothing(c["result"]):
                        made.add(brain._call_key(c["name"], c["arguments"]))
                error = failure(reply, calls)
                if not error:
                    break
                if attempt == 1 and any(c["result"].startswith("User declined") for c in calls):
                    break  # he said no; trying again isn't a different approach
                banned = set(made) | {brain._call_key(c["name"], c["arguments"])
                                      for c in calls if brain._confirms_nothing(c["result"])}
            if error:
                answer = f"I stopped at step {i} ({step}): it didn't work -- {error.rstrip('.')}."
                if replies:
                    answer = "\n\n".join(replies) + "\n\n" + answer
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
    answer = "\n\n".join(r for r in replies if r) or "I went through each step but have nothing to report."
    messages.append({"role": "assistant", "content": answer})
    return answer, messages
