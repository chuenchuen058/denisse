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
FIXTURES = {  # synthetic, clearly not his data
    ("recall", '{"query": "exams"}'): "Error: nothing saved under 'exams'",
    ("search_vault", '{"query": "exams"}'): "[fixture] Note 'Exams': Signals paper on 12 Jan.",
}
ran = []


def fake_tool(name, args, confirm=None, used_once=None):
    ran.append((name, args))
    return FIXTURES.get((name, json.dumps(args, sort_keys=True)), f"[fixture] {name} ok")


class FakeOllama:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def chat(self, model, messages, tools=None, **kw):
        self.sent.append({"messages": list(messages), "tools": [t["function"]["name"] for t in tools or []]})
        return {"message": self.replies.pop(0)}


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
    _, out, _ = respond(history, [calls("recall", query="exams"), {"content": "You have nothing saved about exams."}])
    history = out
    history.append(user("thats not what im asking", f"{n}-complaint"))
    ran.clear()
    reply, out, fake = respond(history, [
        calls("recall", query="exams"),           # the miss, attempted again
        calls("search_vault", query="exams"),     # then something different
        {"content": "Your notes say the Signals paper is on 12 Jan."},
    ])
    history = out
    history.append(user(approval, f"{n}-approval"))
    history.append({"role": "assistant", "content": "Glad that helped."})
    return history, reply, fake


history, reply, fake = episode(1)
check("repair: the failed call is not repeated unchanged",
      ran == [("search_vault", {"query": "exams"})], str(ran))
refused = [m for m in fake.sent[1]["messages"] if m.get("role") == "tool"]
check("repair: model told the repeat was refused",
      bool(refused) and refused[-1]["content"].startswith("Refused: this exact 'recall'"))
check("repair: reply names the original request and the fixed result",
      reply.startswith(f'You asked: "{ASK}"') and "12 Jan" in reply, reply)
turns = learning.label_turns(history)
check("repair: stored complaint turn keeps the repair's tools",
      turns[1]["tools"][-1] == "search_vault", str(turns[1]["tools"]))
check("labels: original bad, repair good after 'thats it'",
      [t["label"] for t in turns[:2]] == ["bad", "good"], str([(t["label"], t["reason"]) for t in turns]))

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
      got and got[0]["name"] == "recall" and got[0]["result"].startswith("Error"), str(got))
prose = [{"role": "user", "content": ASK}, {"role": "assistant", "content": "You have nothing saved about exams."},
         {"role": "user", "content": "thats not what im asking"}]
ran.clear()
reply, _, _ = respond(prose, [calls("recall", query="exams"), calls("search_vault", query="exams"),
                              {"content": "Signals on 12 Jan."}])
check("prose-only history: repeat still refused via disk",
      ran == [("search_vault", {"query": "exams"})], str(ran))

ran.clear()
reply, _, _ = respond(prose, [{"content": "You have nothing saved about exams."}])
check("the same answer again is reported as not fixed", "haven't fixed it" in reply, reply)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
