"""request -> wrong result -> complaint -> repair -> approval -> lesson -> later use.

The model is a scripted fake and tools are harmless fixtures, so this checks
the wiring, not llama3.1. Every store is temporary: no real history, vault
or lesson is read or written.

    .venv\\Scripts\\python.exe scripts\\test_feedback_repair.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="denisse-repair-"))
os.environ["DENISSE_RULES_FILE"] = str(TMP / "rules.json")
(TMP / "rules.json").write_text("{}", encoding="utf-8")
os.environ.setdefault("DENISSE_VAULT", str(TMP / "vault"))

import brain  # noqa: E402
import conversation  # noqa: E402
import instincts  # noqa: E402
import learning  # noqa: E402
import memory_store  # noqa: E402
import tool_router  # noqa: E402

instincts.INSTINCTS_FILE = str(TMP / "instincts.json")
instincts.INSTINCTS_MD = str(TMP / "instincts.md")
memory_store.MEMORY_DIR = str(TMP)
memory_store.MEMORY_FILE = str(TMP / "conversation.json")

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


ASK = "what did i save about my exams"
SCHEMA = {d["function"]["name"]: d["function"]["parameters"] for d in brain.TOOL_DEFINITIONS}
# The real tools' own wording (tools.recall, vault_search), not "Error: ...":
# fixtures that said "Error" passed while the real "found nothing" didn't.
MISS = "I don't have anything remembered for 'exams'. Facts I do have are filed under: home_city."
FIXTURES = {  # synthetic, clearly not his data
    ("recall", '{"key": "exams"}'): MISS,
    ("search_vault", '{"query": "exams"}'): "[fixture] Note 'Exams': Signals paper on 12 Jan.",
    ("get_weather", '{"location": "Tokyo"}'): "Error: weather service unreachable",
    ("add_task", '{"text": "email ben"}'): "Added task: email ben",
    ("list_known_facts", '{}'): "home_city: Penang",
}
ran, unexpected = [], []


def fake_tool(name, args, confirm=None, used_once=None):
    """Strict: real schema, and only calls a fixture exists for."""
    schema = SCHEMA.get(name) or {}
    props, req = schema.get("properties") or {}, schema.get("required") or []
    key = (name, json.dumps(args, sort_keys=True))
    if name not in SCHEMA or set(args) - set(props) or any(k not in args for k in req) or key not in FIXTURES:
        unexpected.append(key)
        return f"Error: unexpected call {key} in this test."
    ran.append((name, args))
    return FIXTURES[key]


class FakeOllama:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def chat(self, model, messages, tools=None, **kw):
        self.sent.append({"messages": list(messages), "tools": [t["function"]["name"] for t in tools or []]})
        return {"message": self.replies.pop(0)}  # IndexError = an unscripted model call


def calls(name, **args):
    return {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def respond(messages, replies):
    fake, real_o, real_t = FakeOllama(replies), brain.ollama, brain._run_tool
    brain.ollama, brain._run_tool = fake, fake_tool
    try:
        b = brain.Brain.__new__(brain.Brain)
        reply, out = b.respond([dict(m) for m in messages])
    finally:
        brain.ollama, brain._run_tool = real_o, real_t
    return reply, out, fake


def user(text, tid):
    return {"role": "user", "content": text, "turn_id": tid}


def episode(n, approval="thats it"):
    """One full live sequence; returns the stored history for it."""
    history = [user(ASK, f"{n}-ask")]
    _, out, _ = respond(history, [calls("recall", key="exams"), {"content": "You have nothing saved about exams."}])
    history = out
    history.append(user("thats not what im asking", f"{n}-complaint"))
    ran.clear()
    reply, out, fake = respond(history, [{"content": "Your notes say the Signals paper is on 12 Jan."}])
    history = out
    history.append(user(approval, f"{n}-approval"))
    history.append({"role": "assistant", "content": "Glad that helped."})
    return history, reply, fake


history, reply, fake = episode(1)
check("recovery: the other permitted source is checked, once, with valid args",
      ran == [("search_vault", {"query": "exams"})] and not unexpected, str(ran) + str(unexpected))
check("recovery: one model call, and it saw the evidence",
      len(fake.sent) == 1 and any("12 Jan" in (m.get("content") or "") for m in fake.sent[0]["messages"]))
check("recovery: the failed recall is not run again", ("recall", {"key": "exams"}) not in ran)
check("repair: reply names the original request and the fixed result",
      reply.startswith(f'You asked: "{ASK}"') and "12 Jan" in reply, reply)
turns = learning.label_turns(history)
check("repair: stored complaint turn keeps the repair's tools",
      "search_vault" in turns[1]["tools"], str(turns[1]["tools"]))
check("labels: original bad, repair good after 'thats it'",
      [t["label"] for t in turns[:2]] == ["bad", "good"], str([(t["label"], t["reason"]) for t in turns]))

# The model ignoring the evidence it was handed is caught, not passed on.
h = history[:4] + [user("thats not what im asking", "x")]
ran.clear()
reply, _, _ = respond(h, [{"content": "Sorry, I don't have anything about your exams."}])
check("recovery: 'I don't have anything' despite evidence is replaced by the evidence",
      "12 Jan" in reply and "don't have" not in reply, reply)

# Every source unavailable: say where she looked, one question, no model call.
real_fx = dict(FIXTURES)
FIXTURES[("search_vault", '{"query": "exams"}')] = "I can't reach the vault (X isn't there), so I can't check."
reply, out, fake = respond(h, [])
check("blocked: names the blocker and asks one question, no model call",
      "couldn't reach your notes" in reply and reply.rstrip().endswith("?") and not fake.sent, reply)
check("blocked: no invented result", "12 Jan" not in reply)
FIXTURES.clear()
FIXTURES.update(real_fx)

# Unrelated saved facts count as nothing found (live: gym -> coffee order).
facts = [user("is my exams info saved anywhere", "f"), {"role": "assistant", "content": "", "tool_calls": [
    {"function": {"name": "list_known_facts", "arguments": {}}}]}, {"role": "tool", "content": "home_city: Penang"},
         {"role": "assistant", "content": "Nothing about exams."}, user("that's wrong", "f2")]
ran.clear()
reply, _, _ = respond(facts, [{"content": "Signals paper on 12 Jan."}])
check("recovery: unrelated facts -> vault checked", ran == [("search_vault", {"query": "exams"})], str(ran))

# A completed action is never redone because he disliked the reply.
act = [user("add a task to email ben", "a"), {"role": "assistant", "content": "", "tool_calls": [
    {"function": {"name": "add_task", "arguments": {"text": "email ben"}}}]}, {"role": "tool", "content": "Added task: email ben"},
       {"role": "assistant", "content": "Sure! Anything else?"}, user("thats not what im asking", "a2")]
ran.clear()
reply, out, fake = respond(act, [])
check("done action: not repeated, no model call, says so and asks",
      not ran and not fake.sent and "haven't done it again" in reply and reply.endswith("?"), reply)

# A failed non-lookup call: the unchanged repeat is refused in the rerun.
wx = [user("whats the weather in tokyo", "w"), {"role": "assistant", "content": "", "tool_calls": [
    {"function": {"name": "get_weather", "arguments": {"location": "Tokyo"}}}]},
      {"role": "tool", "content": "Error: weather service unreachable"},
      {"role": "assistant", "content": "It's sunny."}, user("thats not what im asking", "w2")]
ran.clear()
reply, _, fake = respond(wx, [calls("get_weather", location="Tokyo"),
                              {"content": "The weather service is down, so I can't check Tokyo right now."}])
check("rerun: unchanged failed call refused, not run",
      not ran and any((m.get("content") or "").startswith("Refused: this exact 'get_weather'")
                      for m in fake.sent[1]["messages"]), str(ran))

# A read that found something, rejected: the evidence goes back, not a rerun.
meet = [user("when is the meeting", "m"), {"role": "assistant", "content": "", "tool_calls": [
    {"function": {"name": "search_vault", "arguments": {"query": "meeting"}}}]},
        {"role": "tool", "content": "Note 'Sponsor meeting': 3 Nov. Note 'Supervisor meeting': 10 Nov."},
        {"role": "assistant", "content": "Your sponsor meeting is on 3 Nov."}, user("thats not what im asking", "m2")]
ran.clear()
reply, out, fake = respond(meet, [{"content": "Your sponsor meeting is on 3 Nov."}])
sent = fake.sent[0]["messages"]
check("rejected read: evidence and the rejected answer handed back, nothing re-run",
      not ran and any("Supervisor meeting" in (m.get("content") or "") for m in sent)
      and any(m.get("role") == "system" and "was not what he asked" in m.get("content", "") for m in sent))
check("rejected read: same answer again is reported as not fixed", "haven't fixed it" in reply, reply)
check("rejected read: the handed-back note is not stored twice",
      sum(1 for m in out if m.get("role") == "tool") == 1 and not any(m.get("role") == "system" for m in out))

# Before 2026-10-30 the repair's tools were dropped: the same sequence then
# looked like a bare claim and could never confirm a lesson.
stripped = [m for m in history if not (m.get("role") == "tool" or m.get("tool_calls"))
            or history.index(m) < 3]
check("regression shape: without its tools the repair is unlearnable",
      instincts.derive(learning.label_turns(stripped)) == [])

# -- minimal pairs: what he says after the repair --------------------------
for said, promotes in (("thats it", True), ("perfect", True), ("thats what i wanted", True),
                       ("is that it?", False), ("yes please", False), ("okay", False),
                       ("thats not it", False), ("what about tomorrow", False)):
    h, _, _ = episode(f"mp-{said}", approval=said)
    got = instincts.derive(learning.label_turns(h))
    check(f"minimal pair: {said!r} -> {'candidate' if promotes else 'no lesson'}",
          bool(got) == promotes, str([(t['label'], t['reason']) for t in learning.label_turns(h)]))

# -- confirmations: one episode is a candidate, rereads don't inflate -------
one, _, _ = episode(1)
instincts.rebuild(one)
instincts.rebuild(one)
instincts.rebuild(one + one)  # the same turns read back twice
items = instincts.load()
check("one accepted repair = one confirmation, however often reread",
      len(items) == 1 and items[0]["count"] == 1 and items[0]["phrase"] == instincts.phrase(ASK), str(items))
offered = [d["function"]["name"] for d in tool_router.select(brain.TOOL_DEFINITIONS, [{"role": "user", "content": ASK}])]
check("one confirmation changes nothing yet", not instincts.preferred(ASK, set(offered)))

# Two complaints before one working repair: still one confirmation, and no
# lesson keyed on the complaint text itself.
double = [user(ASK, "d-ask"), {"role": "assistant", "content": "Nothing saved."},
          user("thats not what im asking", "d-c1"), {"role": "assistant", "content": "Nothing saved."},
          user("still not what im asking", "d-c2"),
          {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "search_vault", "arguments": {}}}]},
          {"role": "tool", "content": "[fixture] Note 'Exams'"},
          {"role": "assistant", "content": "Signals on 12 Jan."},
          user("thats it", "d-ok")]
got = instincts.derive(learning.label_turns(double))
check("double complaint: no lesson keyed on a complaint",
      all(e["phrase"] == instincts.phrase(ASK) for e in got) and len(got) == 1 and got[0]["count"] == 1, str(got))

# -- end to end: a second independent accepted repair changes routing ------
two, _, _ = episode(2)
instincts.rebuild(one + two)
order = [d["function"]["name"] for d in tool_router.select(brain.TOOL_DEFINITIONS, [{"role": "user", "content": ASK}])]
check("two accepted repairs: search_vault offered first for the same request",
      order[0] == "search_vault", str(order[:3]))
other = [d["function"]["name"] for d in tool_router.select(brain.TOOL_DEFINITIONS, [{"role": "user", "content": "find my cv"}])]
check("an unrelated request is unaffected", other[0] != "search_vault" or "search_vault" not in other, str(other[:3]))

# A later correction of the learned behaviour demotes it (negative feedback).
later = [user(ASK, "n1"),
         {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "search_vault", "arguments": {}}}]},
         {"role": "tool", "content": "[fixture] irrelevant"}, {"role": "assistant", "content": "x"},
         user("thats not what im asking", "n2"), {"role": "assistant", "content": "y"}]
instincts.rebuild(one + two + later)
check("one negative against two positives demotes it",
      not instincts.preferred(ASK, {"search_vault", "recall"}))

# -- learned lessons never grant action powers -----------------------------
grab = [{"phrase": "make it quiet", "when": ["make", "quiet"], "use": ["run_shell_command", "pause_music"],
         "avoid": [], "count": 9, "validated": True, "evidence_ids": [str(i) for i in range(9)]}]
Path(instincts.INSTINCTS_FILE).write_text(json.dumps({"instincts": grab}), encoding="utf-8")
check("a confirmed lesson cannot add shell or music actions",
      not instincts.adjust({"calculate"}, "make it quiet") & {"run_shell_command", "pause_music"})

# -- the tray path: the failed calls come from disk, not from replay() -----
memory_store.save_history([user(ASK, "disk"), *[m for m in history[1:4]]])
got = conversation.last_attempt(ASK)
check("last_attempt reads the stored call and its result",
      got and got[0]["name"] == "recall" and got[0]["result"] == MISS, str(got))
memory_store.save_history([user(ASK, "disk"), *history[1:4], user("thats not what im asking", "c")])
check("last_attempt skips his complaints about the same request", bool(conversation.last_attempt(ASK)))
memory_store.save_history([user(ASK, "old"), *history[1:4], user("whats the time", "t"),
                           {"role": "assistant", "content": "9pm"}])
check("last_attempt never attaches to an older turn with the same words",
      conversation.last_attempt(ASK) == [])
memory_store.save_history([user(ASK, "disk"), *history[1:4]])
prose = [{"role": "user", "content": ASK}, {"role": "assistant", "content": "You have nothing saved about exams."},
         {"role": "user", "content": "thats not what im asking"}]
ran.clear()
reply, _, _ = respond(prose, [{"content": "Signals on 12 Jan."}])
check("prose-only history: recovery still runs via disk",
      ran == [("search_vault", {"query": "exams"})] and "12 Jan" in reply, str(ran))
check("no unexpected or invalid tool call anywhere", not unexpected, str(unexpected))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
