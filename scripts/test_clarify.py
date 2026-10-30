"""A rejected answer with several plausible ones: she asks which, then answers.

Through Brain.respond() in the tray's own flow (conversation.replay() ->
respond -> conversation.record), with a scripted fake model (an
unscripted call fails), strict fixture tools checked against the real
schemas, and temporary conversation and lesson stores. Synthetic data.

    .venv\\Scripts\\python.exe scripts\\test_clarify.py
"""
import os
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="denisse-clarify-"))
os.environ["DENISSE_RULES_FILE"] = str(TMP / "rules.json")
(TMP / "rules.json").write_text("{}", encoding="utf-8")
os.environ.setdefault("DENISSE_VAULT", str(TMP / "vault"))

import brain  # noqa: E402
import clarify  # noqa: E402
import conversation  # noqa: E402
import instincts  # noqa: E402
import memory_store  # noqa: E402
import planner  # noqa: E402

instincts.INSTINCTS_FILE = str(TMP / "instincts.json")
instincts.INSTINCTS_MD = str(TMP / "instincts.md")
memory_store.MEMORY_DIR = str(TMP)
memory_store.MEMORY_FILE = str(TMP / "conversation.json")
SCHEMA = {d["function"]["name"]: d["function"]["parameters"] for d in brain.TOOL_DEFINITIONS}
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def vault(*notes):
    blocks = ["\n".join([path] + [f"  - {line}" for line in lines]) for path, lines in notes]
    return ("From your notes (quote these, don't embellish; if they don't answer the question, say so):\n\n"
            + "\n\n".join(blocks))


MEETINGS = vault(("Work/Sponsor meeting.md", ["Sponsor meeting with Ben on 3 November, 10am, Zoom."]),
                 ("Uni/Supervisor meeting.md", ["Supervisor meeting on 6 November, 2pm, room S2."]))
LABS = vault(("Uni/Labs.md", ["Electronics lab session: 4 November, 9am.", "Optics lab session: 11 November, 1pm.",
                              "Robotics lab session: 25 November, 3pm."]))
DENTIST = vault(("Health/Appointments.md", ["Dentist check-up on 4 November at 3pm."]))
TOOLS = {"log": [], "allowed": {"search_vault", "recall"}}


def fake_tool(name, args, confirm=None, used_once=None):
    schema = SCHEMA.get(name)
    if name not in TOOLS["allowed"] or schema is None:
        raise AssertionError(f"unexpected tool call {name}({args})")
    props, req = schema.get("properties") or {}, schema.get("required") or []
    if set(args) - set(props) or any(not str(args.get(k) or "").strip() for k in req):
        raise AssertionError(f"invalid arguments for {name}: {args}")
    TOOLS["log"].append((name, args))
    if name == "recall":
        return f"I don't have anything remembered for '{args['key']}'. Facts I do have are filed under: home_city."
    if name == "add_task":
        return f"Added task: {args['text']}"
    return f"Nothing in your notes mentions {args.get('query')}. This searched the Obsidian vault only."


class FakeOllama:
    def __init__(self, replies=()):
        self.replies, self.sent = list(replies), []

    def chat(self, model=None, messages=None, tools=None, **kw):
        self.sent.append(messages)
        if not self.replies:
            raise AssertionError("unscripted model call: " + str((messages or [{}])[-1].get("content"))[:120])
        return {"message": {"content": self.replies.pop(0)}}


def seed(ask, tool, args, evidence, rejected):
    memory_store.save_history([])
    for f in (instincts.INSTINCTS_FILE, instincts.INSTINCTS_MD):
        if os.path.exists(f):
            os.remove(f)
    TOOLS["log"].clear()
    conversation.record([{"role": "user", "content": ask},
                         {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": tool, "arguments": args}}]},
                         {"role": "tool", "content": evidence}, {"role": "assistant", "content": rejected}])


def say(text, replies=()):
    """One tray turn. Returns (reply, fake model)."""
    fake = FakeOllama(replies)
    real = brain.ollama, brain._run_tool
    brain.ollama, brain._run_tool = fake, fake_tool
    try:
        msgs = conversation.replay() + [{"role": "user", "content": text}]
        reply, out = brain.Brain.__new__(brain.Brain).respond([dict(m) for m in msgs])
        conversation.record(out[len(msgs) - 1:])
    finally:
        brain.ollama, brain._run_tool = real
    return reply, fake


def meeting():
    seed("when is my meeting", "search_vault", {"query": "meeting"}, MEETINGS,
         "Your sponsor meeting is on 3 November at 10am, on Zoom.")


meeting()
q, fake = say("that's not what i meant")
check("asks about the other one, by its name in the evidence", q == "Do you mean the supervisor meeting?", q)
check("...without the model, a tool, or the rejected answer", not fake.sent and not TOOLS["log"] and "3 November" not in q)
check("...and replay keeps her question for his answer",
      [m["content"] for m in conversation.replay() if m["role"] == "assistant"][-1:] == [q], conversation.replay())
r, fake = say("yes", ["The supervisor meeting is on 6 November at 2pm, in room S2."])
check("'yes' answers the original request from the chosen evidence", "6 November" in r and "3 November" not in r, r)
sent = " ".join(str(m.get("content")) for m in fake.sent[0])
check("...and the model saw only that item", "Supervisor meeting on 6 November" in sent and "Sponsor" not in sent
      and "3 November" not in sent, sent[-300:])
r, fake = say("yes", ["Sure."])
check("the question is answered once: a second 'yes' is an ordinary message", r == "Sure." and len(fake.sent) == 1, r)
check("no lesson is learned from a clarification", not os.path.exists(instincts.INSTINCTS_FILE)
      or instincts.load() == [], instincts.load() if os.path.exists(instincts.INSTINCTS_FILE) else "")

meeting()
say("that's not what i meant")
r, _ = say("yes", ["Your sponsor meeting is on 3 November."])
check("a model answer that repeats the rejected one is replaced by the evidence",
      "6 November" in r and "3 November" not in r, r)
meeting()
say("that's not what i meant")
r, _ = say("yes", ["The supervisor meeting is on 7 November."])
check("a model answer with a date not in the evidence is replaced", "6 November" in r and "7 November" not in r, r)
meeting()
say("that's not what i meant")
r, _ = say("the supervisor one", [])
check("model down: the answer is the evidence itself", r.startswith("Here's what I found for the supervisor meeting")
      and "6 November" in r, r)

seed("when is my lab session", "search_vault", {"query": "lab session"}, LABS,
     "Your electronics lab session is on 4 November at 9am.")
q, _ = say("thats not what im asking")
check("three items, one rejected: names the other two", q == "Which one do you mean: the optics lab session or the "
      "robotics lab session?", q)
labels = [o["label"] for o in clarify._pending["options"]]
check("every name she offers is copied from the evidence", all(lab.lower() in LABS.lower() for lab in labels), labels)
r, _ = say("optics", ["The optics lab session is on 11 November at 1pm."])
check("a word from one option picks it", "11 November" in r, r)

seed("when is my lab session", "search_vault", {"query": "lab session"}, LABS,
     "Your electronics lab session is on 4 November at 9am.")
say("thats not what im asking")
r, _ = say("yes")
check("'yes' to a two-way question asks again, doesn't guess", r == q, r)
r, _ = say("the fourth one")
check("an option that doesn't exist: says so and asks again", r.startswith("That isn't one I found.") and "?" in r, r)
r, _ = say("robotics", ["The robotics lab session is on 25 November at 3pm."])
check("...and the re-asked question still resolves", "25 November" in r, r)

meeting()
say("that's not what i meant")
r, fake = say("whats the time", ["It's late."])
check("an unrelated message goes on as a new request", r == "It's late.", r)
r, fake = say("yes", ["Yes to what?"])
check("...and an old 'yes' afterwards no longer answers the question", r == "Yes to what?" and "6 November" not in r, r)

meeting()
say("that's not what i meant")
r, _ = say("thanks", ["No problem."])
check("'thanks' after her question goes on as before (the model answers it)", r == "No problem.", r)
r, _ = say("yes", ["Mm?"])
check("...so a later 'yes' resolves nothing", r == "Mm?", r)

meeting()
say("that's not what i meant")
r, _ = say("no, the sponsor one", ["Your sponsor meeting is on 3 November at 10am, on Zoom."])
check("naming the first one explicitly picks it, from its evidence", "3 November" in r and "isn't one I found" not in r, r)

meeting()
say("that's not what i meant")
r, _ = say("no")
check("'no': says what was found and what's missing, without dates",
      "sponsor meeting" in r and "supervisor meeting" in r and "?" in r and "November" not in r, r)

meeting()
say("that's not what i meant")
real_expiry, clarify.EXPIRY_SECONDS = clarify.EXPIRY_SECONDS, -1
try:
    r, _ = say("yes", ["Yes to what?"])
finally:
    clarify.EXPIRY_SECONDS = real_expiry
check("an expired question answers nothing", r == "Yes to what?", r)

seed("when is my dentist appointment", "search_vault", {"query": "dentist"}, DENTIST,
     "Your dentist check-up is on 4 November at 3pm.")
r, fake = say("thats not what i meant")
check("one item, rejected: checks one other source, then says what's missing",
      TOOLS["log"] == [("recall", {"key": "dentist appointment"})] and "dentist check-up" in r
      and "4 November" not in r and r.endswith("?") and not fake.sent, f"{TOOLS['log']} | {r}")
check("...and leaves no question open", clarify._pending is None)

memory_store.save_history([])
TOOLS["log"].clear()
TOOLS["allowed"] = {"add_task"}
conversation.record([{"role": "user", "content": "add a task to email ben"},
                     {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "add_task", "arguments": {"text": "email ben"}}}]},
                     {"role": "tool", "content": "Added task: email ben"}, {"role": "assistant", "content": "Done!"}])
r, fake = say("thats not what im asking")
check("a completed action is not run again", "already done" in r and TOOLS["log"] == [] and not fake.sent, r)
TOOLS["allowed"] = {"search_vault", "recall"}

real_pending, real_accept = planner.pending, planner.accept
accepted = []
planner.pending, planner.accept = (lambda: True), (lambda: accepted.append(1) or "Added to your tasks.")
try:
    meeting()
    say("that's not what i meant")
    r, _ = say("yes", ["The supervisor meeting is on 6 November."])
    check("with a plan also pending, 'yes' answers her question, not the plan", "6 November" in r and not accepted, r)
    memory_store.save_history([{"role": "user", "content": "plan my week"},
                               {"role": "assistant", "content": "Want me to add these to your tasks?"}])
    r, _ = say("yes")
    check("a plan confirmation still works on its own", accepted == [1] and r == "Added to your tasks.", r)
finally:
    planner.pending, planner.accept = real_pending, real_accept

clarify.offer("a", "Do you mean the first?", [{"label": "first", "text": "first"}], [], "x")
clarify.offer("b", "Do you mean the second?", [{"label": "second", "text": "second"}], [], "x")
check("a new question replaces the old one", clarify.take("Do you mean the first?") is None)
clarify.offer("b", "Do you mean the second?", [{"label": "second", "text": "second"}], [], "x")
got, barrier = [], threading.Barrier(8)


def take():
    barrier.wait()
    got.append(clarify.take("Do you mean the second?"))


threads = [threading.Thread(target=take) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("eight simultaneous answers: taken once", sum(g is not None for g in got) == 1, got)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
