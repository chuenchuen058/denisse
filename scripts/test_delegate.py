"""The hand-off to Claude: where the line is, and what reaches Claude.

No real Claude or Codex is run: shutil.which and subprocess.run are faked,
DRAFTS and the log point at a temp folder, so nothing is spent and nothing
is written to his vault or his real delegate.log. The prompts below are shaped like his real ones, not copies.
"""
import os
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import brain  # noqa: E402
import delegate  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# -- the line: Claude's jobs ------------------------------------------------
for text in ("could you write me a cover letter for JP Morgan London",
             "what should i add or change for the current cv",
             "could you search the web on internships due before november in singapore",
             "give me at least 10 places each to apply to",
             "could you check prof dario farina, search it up",
             "prep me on what to say for the pitch",
             "could you add the solutions to electromagnetism week 5",
             "how to do my signal and system coursework",
             "okay could you draft me the post",
             "ask claude to tidy this paragraph"):
    check(f"hand off: {text!r}", brain._should_delegate(text))

# -- ...and what stays local ------------------------------------------------
for text in ("pull up my latest cv", "find my cv", "open a file called passport",
             "remind me to write a cover letter tomorrow", "plan my study modules",
             "tell me the briefing", "hows my internship tracker", "hey",
             "what do you know about the projects im doing?",
             "could you help me plan my internship applications",
             "what time is it in tokyo", "improve the brightness of my screen"):
    check(f"local: {text!r}", not brain._should_delegate(text))

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
