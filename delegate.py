"""Denisse -- hand the big jobs to Claude (or Codex) and bring back a file.

Most of what Chuen actually asks AI for (2026-10-29, from ~180 of his own
prompts to Claude Code and Codex) is CV and cover-letter writing,
internship research, pitch prep and coursework solutions. An 8B local
model does those badly, and badly in the worst way: plausible, generic,
confident -- the "reply to satisfy me" he said he doesn't want.

So Denisse doesn't attempt them. She hands the job to Claude Code on his
laptop, headless, and saves what comes back as a note in his vault under
Denisse/Drafts/. Local, checkable work (files, notes, tasks, deadlines)
still never leaves the machine -- see brain._DELEGATE for the line.

Claude gets read-only access plus web search: it can read his vault,
documents and GitHub folder and search online. It cannot edit files or
run commands -- write tools and Bash are explicitly disallowed. The
finished deliverable comes back as text and this module writes the file.

Runs in a background thread: these jobs take a minute or two, and the
tray must not freeze. on_done(message) is called when one finishes.
"""
import datetime as dt
import os
import re
import shutil
import subprocess
import threading

HOME = os.path.expanduser("~")
VAULT = os.path.join(HOME, "OneDrive - King's College London", "Denisse")
DRAFTS = os.path.join(VAULT, "Denisse", "Drafts")
READ_DIRS = [VAULT, os.path.join(HOME, "OneDrive", "Documents"),
             os.path.join(HOME, "OneDrive", "Desktop"), os.path.join(HOME, "GitHub"),
             os.path.join(HOME, "OneDrive - King's College London", "Documents")]
TIMEOUT_S = 600

ALLOWED = "Read,Glob,Grep,WebSearch,WebFetch"
DISALLOWED = "Edit,Write,MultiEdit,NotebookEdit,Bash,PowerShell"

DRY_RUN = False   # the eval sets this: decide to hand off, never actually spend
on_done = None    # tray sets this to show the result when a job finishes

SYSTEM = (
    "You are doing a job for Chuen Lim, a second-year Biomedical Engineering "
    "student at King's College London, on behalf of Denisse, his local "
    "assistant. Do the job fully and well, as the finished deliverable -- he "
    "wants the task done, not a description of how he could do it. You can "
    "read his files and search the web; you cannot edit files, so return the "
    "deliverable as your answer, in Markdown. Never invent facts about him: "
    "use only what is in his files or what he said. Don't ask him questions "
    "back. If something you need is missing, make the most sensible "
    "assumption and say so in one line at the end, under 'Assumptions'."
)


def _prompt(task: str, context: list) -> str:
    where = "\n".join(f"- {d}" for d in READ_DIRS if os.path.isdir(d))
    convo = "\n".join(f"{m['role']}: {m['content'][:400]}" for m in context[-6:]) or "(none)"
    return (f"He asked Denisse: \"{task}\"\n\n"
            f"Recent conversation, for context:\n{convo}\n\n"
            f"His files are in:\n{where}\n"
            f"(His Obsidian vault holds his notes, module pages, coursework, "
            f"internship research and CV-related notes.)")


def _command(engine: str, prompt: str, outfile: str = "") -> tuple:
    """(argv, stdin_bytes) for one engine, or (None, None) if not installed."""
    if engine == "claude":
        exe = shutil.which("claude")
        if not exe:
            return None, None
        cmd = [exe, "-p", prompt, "--output-format", "text",
               "--append-system-prompt", SYSTEM,
               "--allowedTools", ALLOWED, "--disallowedTools", DISALLOWED]
        for d in READ_DIRS:
            if os.path.isdir(d):
                cmd += ["--add-dir", d]
        return cmd, None
    if engine == "codex":
        exe = shutil.which("codex")
        if not exe:
            return None, None
        # Through stdin, never argv. Codex on Windows is a .cmd wrapper, and
        # a multi-line argument through a .cmd is cut at the first line
        # break: on 2026-10-29 Codex received only the instructions, replied
        # "no specific job or deliverable", and that was saved as the draft.
        # -o writes just its final message, without the progress log.
        cmd = [exe, "exec", "--sandbox", "read-only", "--skip-git-repo-check",
               "-C", HOME, "-o", outfile, "-"]
        return cmd, (SYSTEM + "\n\n" + prompt).encode("utf-8")
    return None, None


# A reply that isn't the deliverable. Saving one as "Done" would be exactly
# the satisfy-not-do reply he asked her to stop -- it happened once, on the
# day this file was written.
_NON_ANSWER = re.compile(
    r"no\s+(?:specific\s+)?(?:job|task|deliverable|instructions?)\b"
    r"|(?:didn'?t|did\s+not|haven'?t|has\s+not)\s+(?:provide|give|include|specif)"
    r"|(?:please|could\s+you|can\s+you)\s+(?:provide|share|specify|clarify|tell\s+me\s+(?:what|which))"
    r"|what\s+would\s+you\s+like\s+me\s+to|task\s+description\s+was\s+omitted",
    re.IGNORECASE)


def _is_non_answer(text: str) -> bool:
    head = (text or "")[:400]
    return len((text or "").strip()) < 40 or bool(_NON_ANSWER.search(head))


LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory", "delegate.log")


def _log(line: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError:
        pass


# brain.py calls load_dotenv(), so by the time a job runs, Denisse's .env
# is in os.environ -- including ANTHROPIC_API_KEY. A child `claude` that
# inherits it bills that key instead of using his normal Claude login, and
# on 2026-10-29 every in-Denisse hand-off to Claude failed that way and fell
# back to Codex. Run with the environment he'd have typing `claude` himself.
_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


def _child_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _brief(err: str) -> str:
    """The one line of an error worth showing him, not a stack of noise."""
    lines = [l.strip() for l in (err or "").splitlines() if re.search(r"[A-Za-z]{3}", l)]
    key = [l for l in lines if re.search(r"error|disabled|denied|limit|auth|invalid|expired|"
                                         r"credit|quota|not\s+logged", l, re.IGNORECASE)]
    line = (key or lines or ["no output"])[-1]
    return line[:140]


def _decode(raw: bytes | None) -> str:
    """UTF-8 if it is, else the Windows code page. Claude Code writes proper
    UTF-8 (checked: an en dash arrives as e2 80 93); the fallback is for a
    tool that writes the ANSI code page instead."""
    raw = raw or b""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def run(task: str, context: list | None = None, engines=("claude", "codex")) -> tuple:
    """Do the job synchronously. Returns (ok, text, engine, failures):
    `failures` lists every engine that was tried and didn't deliver, and
    why -- a fallback is reported to him, never hidden."""
    prompt = _prompt(task, context or [])
    failures = []
    for engine in engines:
        import tempfile
        outfile = os.path.join(tempfile.gettempdir(), f"denisse-{engine}-{os.getpid()}.md")
        cmd, stdin_bytes = _command(engine, prompt, outfile)
        if not cmd:
            continue
        try:
            if os.path.exists(outfile):
                os.remove(outfile)
            p = subprocess.run(cmd, cwd=HOME, capture_output=True, timeout=TIMEOUT_S,
                               input=stdin_bytes,
                               stdin=None if stdin_bytes is not None else subprocess.DEVNULL,
                               env=_child_env(),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            failures.append(f"{engine} took longer than {TIMEOUT_S // 60} minutes")
            _log(f"{engine} TIMEOUT task={task[:80]!r}")
            continue
        except OSError as e:
            failures.append(f"{engine} couldn't start ({e})")
            _log(f"{engine} OSERROR {e} task={task[:80]!r}")
            continue
        out = _decode(p.stdout).strip()
        if engine == "codex" and os.path.exists(outfile):
            out = _decode(open(outfile, "rb").read()).strip()
        err = _decode(p.stderr).strip()
        if p.returncode == 0 and out and not _is_non_answer(out):
            _log(f"{engine} OK chars={len(out)} task={task[:80]!r}")
            return True, out, engine, failures
        why = ("came back without doing the job" if p.returncode == 0 and out
               else _brief(err or out))
        failures.append(f"{engine}: {why}")
        _log(f"{engine} FAIL rc={p.returncode} why={why!r} task={task[:80]!r}")
    if not failures:
        failures.append("neither Claude Code nor Codex is installed")
    return False, "; ".join(failures), None, failures


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    stop = {"could", "you", "please", "me", "my", "for", "the", "a", "an", "can", "i", "to"}
    return " ".join([w for w in words if w not in stop][:7]) or "draft"


def save(task: str, text: str, engine: str, now: dt.datetime | None = None) -> str:
    """Write the deliverable as a vault note that passes the vault contract
    (frontmatter: name, description, tags, updated). Returns the path."""
    now = now or dt.datetime.now()
    os.makedirs(DRAFTS, exist_ok=True)
    slug = _slug(task)
    name = f"{now:%Y-%m-%d %H%M} {slug}"
    path = os.path.join(DRAFTS, f"{name}.md")
    who = "Claude" if engine == "claude" else "Codex"
    desc = task.replace('"', "'").strip()
    body = (f"---\nname: {re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}\n"
            f"description: \"Draft by {who} via Denisse: {desc[:150]}\"\n"
            f"tags: [denisse, draft]\nupdated: {now:%Y-%m-%d}\n---\n\n"
            f"> [!note] Asked: {desc}\n> Done by {who} on {now:%d %b %Y %H:%M}, "
            f"read-only access to his files and the web. Check it before you send it.\n\n"
            f"{text.strip()}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def start(task: str, context: list | None = None) -> str:
    """Hand the job off in the background. Returns what Denisse says now."""
    if not (shutil.which("claude") or shutil.which("codex")):
        return ("That's a job for Claude or Codex, and neither is installed on "
                "this laptop, so I can't hand it off.")
    if DRY_RUN:
        return ("Handing that to Claude -- it can read your files and search the "
                "web. I'll tell you when it's done and save it in Denisse/Drafts.")

    def work():
        ok, text, engine, failures = run(task, context)
        if ok:
            path = save(task, text, engine)
            who = "Claude" if engine == "claude" else "Codex"
            msg = (f"Done -- {who} finished \"{task[:60]}\". It's in your vault: "
                   f"Denisse/Drafts/{os.path.basename(path)}")
            if failures:
                msg += f" (Claude didn't manage it -- {failures[0][:120]} -- so Codex did.)"
        else:
            msg = (f"I couldn't get that done: {text[:300]}. Nothing was saved. "
                   "Ask me again in a bit, or ask Claude directly.")
        if on_done:
            try:
                on_done(msg)
            except Exception:
                pass

    threading.Thread(target=work, daemon=True).start()
    return ("Handing that to Claude -- it can read your files and search the web. "
            "Usually a minute or two; I'll tell you when it's done, and it'll be "
            "saved in your vault under Denisse/Drafts.")
