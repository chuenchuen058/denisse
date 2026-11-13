"""The hand-off to Claude: who decides, that he is asked, and what reaches Claude.

Since 2026-11-13 (audit 10 Nov, D2) the hand-off is the `delegate` tool: the
local model chooses it, and it never runs without his yes. The brain checks
below use a scripted model and a patched delegate.start, so no hand-off
starts and no model runs.

No real Claude or Codex is run: shutil.which and subprocess.run are faked,
DRAFTS and the log point at a temp folder, so nothing is spent and nothing
is written to his vault or his real delegate.log. The prompts below are shaped like his real ones, not copies.
"""
import os
import subprocess
import sys
import tempfile
import threading
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="denisse-delegate-brain-")
os.environ["DENISSE_RULES_FILE"] = os.path.join(TMP, "rules.json")
with open(os.environ["DENISSE_RULES_FILE"], "w", encoding="utf-8") as _f:
    _f.write("{}")
os.environ.setdefault("DENISSE_VAULT", os.path.join(TMP, "vault"))

import brain  # noqa: E402
import delegate  # noqa: E402
import instincts  # noqa: E402
import memory_store  # noqa: E402
import safety  # noqa: E402
import tool_router  # noqa: E402

instincts.INSTINCTS_FILE = os.path.join(TMP, "instincts.json")
instincts.INSTINCTS_MD = os.path.join(TMP, "instincts.md")
memory_store.MEMORY_DIR = TMP
memory_store.MEMORY_FILE = os.path.join(TMP, "conversation.json")

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# -- the model decides, and he is asked ----------------------------------------
class FakeModel:
    """A scripted model: each chat() returns the next scripted message, and
    the last one again if the brain asks once more (an honesty retry)."""

    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, model, messages, tools=None, **kw):
        return {"message": self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]}


def calls(name, **args):
    return {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


STARTED = "Handing that to Claude -- it can read your files and search the web."


def respond(history, replies, confirm=None):
    """One turn through the real brain; returns (reply, delegate.start calls)."""
    fake = FakeModel(replies)
    starts = []

    def fake_start(task, context=None):
        starts.append((task, context))
        return STARTED

    real = brain.ollama
    brain.ollama = fake
    try:
        with mock.patch.object(delegate, "start", fake_start):
            b = brain.Brain.__new__(brain.Brain)
            reply, _ = b.respond([dict(m) for m in history], confirm=confirm)
    finally:
        brain.ollama = real
    return reply, starts


# Shaped like the requests the old cues.delegate_patterns started a hand-off
# on, bare trigger words included.
OLD_TRIGGERS = ("could you write me a cover letter for JP Morgan London",
                "what should i add or change for the current cv",
                "could you search the web on internships due before november in singapore",
                "give me at least 10 places each to apply to",
                "could you check prof dario farina, search it up",
                "prep me on what to say for the pitch",
                "could you add the solutions to electromagnetism week 5",
                "how to do my signal and system coursework",
                "okay could you draft me the post",
                "ask claude to tidy this paragraph",
                "research", "solve", "prove it", "look it up")

check("no regex short-circuit is left",
      not hasattr(brain, "_should_delegate") and not hasattr(brain, "_DELEGATE"))
check("delegate is a tool the model can call", "delegate" in brain.tools.TOOL_FUNCTIONS
      and "delegate" in {d["function"]["name"] for d in brain.TOOL_DEFINITIONS})
check("delegate is confirmed first", "delegate" in safety.RISKY_TOOLS)


def offered(text):
    return {d["function"]["name"] for d in tool_router.select(
        brain.TOOL_DEFINITIONS, [{"role": "user", "content": text}])}


for text in OLD_TRIGGERS:
    reply, starts = respond([{"role": "user", "content": text}],
                            [{"content": "Happy to help with that here."}], confirm=lambda n, a: True)
    check(f"old trigger words start nothing by themselves: {text!r}", not starts, starts)

for text in OLD_TRIGGERS[:10]:
    check(f"the model is offered the hand-off: {text!r}", "delegate" in offered(text))
for text in ("what time is it in tokyo", "hey", "pull up my latest cv"):
    check(f"not offered for local work: {text!r}", "delegate" not in offered(text))

ASK = "could you write me a cover letter for JP Morgan London  "
HISTORY = [{"role": "user", "content": "i'm applying to banks this week"},
           {"role": "assistant", "content": "Good luck -- want a hand with anything?"},
           {"role": "user", "content": ASK}]
CONTEXT = [{"role": m["role"], "content": m["content"]} for m in HISTORY[:-1]]
asked = []


def answer(yes):
    def confirm(name, args):
        asked.append((name, dict(args)))
        return yes
    return confirm


reply, starts = respond(HISTORY, [calls("delegate", task="cover letter for a bank"),
                                  {"content": "Sure, I'll get started on that!"}], confirm=answer(False))
check("the tool call asks first", [n for n, _ in asked] == ["delegate"], asked)
shown = asked[0][1] if asked else {}
check("...showing his own words as what is sent", shown.get("task") == ASK.strip(), shown)
check("...and which engine it goes to", "Claude" in str(shown.get("engine")), shown)
check("...and the turns sent with it", shown.get("context") == CONTEXT, shown.get("context"))
check("a no starts nothing", not starts, starts)
check("...and says so", "nothing" in reply.lower() and "claude" in reply.lower(), reply)

asked.clear()
reply, starts = respond(HISTORY, [calls("delegate", task="cover letter for a bank"),
                                  {"content": "On it."}], confirm=answer(True))
check("a yes asks once and calls delegate.start once", len(asked) == 1 and len(starts) == 1, starts)
check("...with his message and the turns before it, as the old path did",
      starts == [(ASK.strip(), CONTEXT)], starts)
check("...and she says it went to Claude", "Claude" in reply, reply)

import tray  # noqa: E402

check("unattended calls decline the hand-off", tray._decline_risky_unattended("delegate", {}) is False)
reply, starts = respond(HISTORY, [calls("delegate", task="cover letter"), {"content": "Done!"}],
                        confirm=tray._decline_risky_unattended)
check("...and nothing starts from the unattended path", not starts, starts)

import wake_word  # noqa: E402

voice_gate = wake_word.VOICE_CONFIRM_TOOLS if wake_word.VOICE_CONFIRM_ENABLED else set(safety.RISKY_TOOLS)
check("the voice path asks too", "delegate" in voice_gate)
spoken = wake_word._describe_action("delegate", {"task": "write me a cover letter",
                                                 "engine": "Claude, then Codex", "context": CONTEXT})
check("...saying what goes where", "cover letter" in spoken and "Claude" in spoken, spoken)

# -- what Claude is allowed to do -------------------------------------------
tmp = tempfile.mkdtemp(prefix="denisse-delegate-")
delegate.DRAFTS = os.path.join(tmp, "Drafts")
# The fake runs below log "rate limited" and "write me a cover letter" like
# real ones, and until 2026-10-30 every test run appended them to his real
# memory/delegate.log.
delegate.LOG_FILE = os.path.join(tmp, "delegate.log")
calls = []
real_which, real_run = delegate.shutil.which, delegate.subprocess.run


def fake_which(name):
    return {"claude": r"C:\fake\claude.exe", "codex": r"C:\fake\codex.cmd"}.get(name)


DELIVERABLE = "# Cover letter\n\nDear team \u2013 here is a full letter about the role and why I fit it."
stdin_seen = []


def fake_run(cmd, **kw):
    calls.append(cmd)
    stdin_seen.append(kw.get("input"))
    if "claude.exe" in cmd[0] and fail_claude[0]:
        return subprocess.CompletedProcess(cmd, 1, b"", b"rate limited")
    if "claude.exe" in cmd[0] and lazy_claude[0]:
        return subprocess.CompletedProcess(
            cmd, 0, b"Your message provides instructions but no specific job or deliverable.", b"")
    if "codex" in cmd[0]:
        out = cmd[cmd.index("-o") + 1]
        open(out, "wb").write(DELIVERABLE.encode("utf-8"))
        return subprocess.CompletedProcess(cmd, 0, b"progress log noise", b"")
    return subprocess.CompletedProcess(cmd, 0, DELIVERABLE.encode("utf-8"), b"")


delegate.shutil.which, delegate.subprocess.run = fake_which, fake_run
fail_claude, lazy_claude = [False], [False]
ok, text, engine, failures = delegate.run("write me a cover letter", [])
cmd = calls[-1]
allowed = cmd[cmd.index("--allowedTools") + 1]
denied = cmd[cmd.index("--disallowedTools") + 1]
check("claude is tried first and succeeds", ok and engine == "claude", text)
check("read and web tools only", allowed == "Read,Glob,Grep,WebSearch,WebFetch", allowed)
check("writing and shell are explicitly denied",
      all(t in denied for t in ("Write", "Edit", "Bash")), denied)
check("only folders that exist are shared",
      all(os.path.isdir(cmd[i + 1]) for i, a in enumerate(cmd) if a == "--add-dir"))
check("an en dash survives", "\u2013" in text, text)

fail_claude[0] = True
ok, text, engine, failures = delegate.run("write me a cover letter", [])
check("falls back to codex when claude fails", ok and engine == "codex", engine)
check("the claude failure is reported, not hidden",
      failures and "rate limited" in failures[0], failures)
check("codex runs read-only", "read-only" in calls[-1], calls[-1][:5])
check("codex gets the task on stdin, not argv",
      calls[-1][-1] == "-" and stdin_seen[-1] and b"write me a cover letter" in stdin_seen[-1])
check("codex result is its final message, not its progress log",
      text == DELIVERABLE, text[:60])
fail_claude[0] = False

lazy_claude[0] = True
ok, text, engine, failures = delegate.run("write me a cover letter", [])
check("a non-answer is not accepted as done", engine == "codex" and ok,
      f"{engine} {failures}")
check("...and the non-answer is named as the reason",
      failures and "without doing the job" in failures[0], failures)
lazy_claude[0] = False
for bad in ("Your message provides the working instructions, but no specific job or deliverable.",
            "Could you provide more details about the role?", "OK."):
    check(f"non-answer detected: {bad[:40]!r}", delegate._is_non_answer(bad))
check("a real deliverable is not a non-answer", not delegate._is_non_answer(DELIVERABLE))
os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-real"
env = delegate._child_env()
check("child claude never inherits an API key from Denisse's .env",
      "ANTHROPIC_API_KEY" not in env and env.get("PYTHONIOENCODING") == "utf-8")
del os.environ["ANTHROPIC_API_KEY"]
noisy = ("Permission deny rule matches no known tool - check for typos.\n\x1b[2m\n"
         "claude.ai connectors are disabled because ANTHROPIC_API_KEY or another auth source is set")
check("the error shown is the one line that matters",
      "connectors are disabled" in delegate._brief(noisy), delegate._brief(noisy))
check("cp1252 output is still readable", delegate._decode("caf\xe9".encode("cp1252")) == "caf\u00e9")

# -- saving, and the background job -------------------------------------------
path = delegate.save("write me a cover letter for JP Morgan", "Dear team...", "claude")
body = open(path, encoding="utf-8").read()
check("saved under Drafts", os.path.dirname(path) == delegate.DRAFTS, path)
check("note has the vault's frontmatter",
      all(f"\n{k}:" in "\n" + body for k in ("name", "description", "tags", "updated")))
check("note says who wrote it and to check it",
      "Claude" in body and "Check it before you send it" in body)

delegate.DRY_RUN = True
said = delegate.start("write me a cover letter", [])
check("dry run says it's handing to Claude", "Claude" in said)
delegate.DRY_RUN = False
fail_claude[0] = True
lazy_claude[0] = False
done = threading.Event()
got = []
delegate.on_done = lambda m: (got.append(m), done.set())
delegate.start("write me a cover letter", [])
done.wait(5)
check("fallback is told to him in the done message",
      bool(got) and "so Codex did" in got[0], got[:1])
fail_claude[0] = False
done = threading.Event()
got = []
delegate.on_done = lambda m: (got.append(m), done.set())
said = delegate.start("write me a cover letter", [])
done.wait(5)
check("background job reports back", bool(got) and "Drafts" in got[0], got[:1])
check("she says it straight away, not after", "minute or two" in said)

delegate.shutil.which = lambda name: None
said = delegate.start("write me a cover letter", [])
check("no Claude or Codex -> she says so, doesn't pretend", "neither is installed" in said, said)
delegate.shutil.which, delegate.subprocess.run = real_which, real_run

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
