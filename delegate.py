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

Engines, their arguments, tool lists, prompt wording and every sentence
said about a hand-off live in delegate.json beside this file (Rule 0).
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading

import settings

with open(os.path.splitext(os.path.abspath(__file__))[0] + ".json", encoding="utf-8-sig") as _config_file:
    CONFIG = json.load(_config_file)

HOME = settings.home()
VAULT = settings.vault()
DRAFTS = os.path.join(VAULT, *CONFIG["drafts_folder"].split("/"))
# What a delegated Claude may read: config.json's fixed list, in its order --
# never discovered, since every folder added widens what it can see.
READ_DIRS = [settings.expand(d) for d in settings.get("read_dirs", ["{vault}"])]
LEARNING_NOTE = settings.get("learning_note")
TIMEOUT_S = CONFIG["timeout_s"]

ALLOWED = CONFIG["allowed_tools"]
DISALLOWED = CONFIG["disallowed_tools"]

DRY_RUN = False   # the eval sets this: decide to hand off, never actually spend
on_done = None    # tray sets this to show the result when a job finishes

SYSTEM = CONFIG["system"].format(
    user_name=settings.user_name(),
    about=(CONFIG["system_about"].format(user_about=settings.get("user_about"))
           if settings.get("user_about") else ""))


def _label(engine) -> str:
    return (CONFIG["engines"].get(engine) or {}).get("label", engine)


def _prompt(task: str, context: list) -> str:
    where = "\n".join(CONFIG["read_dir_line"].format(dir=d) for d in READ_DIRS if os.path.isdir(d))
    convo = "\n".join(CONFIG["context_line"].format(role=m["role"], content=m["content"][:CONFIG["context_chars"]])
                      for m in context[-CONFIG["context_turns"]:]) or CONFIG["context_none"]
    return (CONFIG["prompt"].format(task=task, convo=convo, where=where)
            + (CONFIG["prompt_learning"].format(note=LEARNING_NOTE) if LEARNING_NOTE else "")
            + _shared_context())


def _shared_context() -> str:
    """The same live context pack every agent gets (python -m atrium context):
    standing asks, the learner model, what's active, what's settled. It goes to
    Claude/Codex here, never into Denisse's own prompt -- feeding the vault to
    the 8B model measured 0/4 clean replies (AGENTS.md)."""
    try:
        from atrium import context
        return CONFIG["shared_context_separator"] + context.show(CONFIG["shared_context_agent"])
    except Exception:
        return ""


def _command(engine: str, prompt: str, outfile: str = "") -> tuple:
    """(argv, stdin_bytes) for one engine, or (None, None) if it isn't
    configured or installed."""
    spec = CONFIG["engines"].get(engine)
    if not spec:
        return None, None
    exe = shutil.which(spec["exe"])
    if not exe:
        return None, None
    values = {"{prompt}": prompt, "{system}": SYSTEM, "{allowed}": ALLOWED,
              "{disallowed}": DISALLOWED, "{home}": HOME, "{outfile}": outfile}
    cmd = [exe] + [values.get(a, a) for a in spec["args"]]
    if spec.get("add_dir_flag"):
        for d in READ_DIRS:
            if os.path.isdir(d):
                cmd += [spec["add_dir_flag"], d]
    if spec.get("stdin"):
        return cmd, (SYSTEM + CONFIG["stdin_separator"] + prompt).encode("utf-8")
    return cmd, None


def _is_non_answer(text: str) -> bool:
    head = (text or "")[:CONFIG["non_answer_head_chars"]]
    return (len((text or "").strip()) < CONFIG["non_answer_min_chars"]
            or bool(re.search(CONFIG["non_answer_pattern"], head)))


LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), *CONFIG["log_file"].split("/"))


def _log(line: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError:
        pass


def _child_env() -> dict:
    """The environment he'd have typing the command himself: brain.py's
    load_dotenv() puts Denisse's .env into os.environ, and keys listed in
    strip_env must not reach the child."""
    strip = set(CONFIG["strip_env"])
    env = {k: v for k, v in os.environ.items() if k not in strip}
    env.update(CONFIG["child_env"])
    return env


def _brief(err: str) -> str:
    """The one line of an error worth showing him, not a stack of noise."""
    lines = [l.strip() for l in (err or "").splitlines() if re.search(CONFIG["error_line_pattern"], l)]
    key = [l for l in lines if re.search(CONFIG["error_key_pattern"], l)]
    line = (key or lines or [CONFIG["no_output"]])[-1]
    return line[:CONFIG["error_chars"]]


def _decode(raw: bytes | None) -> str:
    """UTF-8 if it is, else the Windows code page. Claude Code writes proper
    UTF-8 (checked: an en dash arrives as e2 80 93); the fallback is for a
    tool that writes the ANSI code page instead."""
    raw = raw or b""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(CONFIG["fallback_codepage"], errors="replace")


def run(task: str, context: list | None = None, engines=None) -> tuple:
    """Do the job synchronously. Returns (ok, text, engine, failures):
    `failures` lists every engine that was tried and didn't deliver, and
    why -- a fallback is reported to him, never hidden."""
    engines = list(CONFIG["engines"]) if engines is None else engines
    fail, log = CONFIG["failures"], CONFIG["log"]
    short = task[:CONFIG["log_task_chars"]]
    prompt = _prompt(task, context or [])
    failures = []
    for engine in engines:
        outfile = os.path.join(tempfile.gettempdir(),
                               CONFIG["outfile"].format(engine=engine, pid=os.getpid()))
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
            failures.append(fail["timeout"].format(engine=engine, minutes=TIMEOUT_S // 60))
            _log(log["timeout"].format(engine=engine, task=short))
            continue
        except OSError as e:
            failures.append(fail["oserror"].format(engine=engine, error=e))
            _log(log["oserror"].format(engine=engine, error=e, task=short))
            continue
        out = _decode(p.stdout).strip()
        if CONFIG["engines"][engine].get("reads_outfile") and os.path.exists(outfile):
            out = _decode(open(outfile, "rb").read()).strip()
        err = _decode(p.stderr).strip()
        if p.returncode == 0 and out and not _is_non_answer(out):
            _log(log["ok"].format(engine=engine, chars=len(out), task=short))
            return True, out, engine, failures
        why = (fail["non_answer"] if p.returncode == 0 and out
               else _brief(err or out))
        failures.append(fail["fail"].format(engine=engine, why=why))
        _log(log["fail"].format(engine=engine, rc=p.returncode, why=why, task=short))
    if not failures:
        failures.append(fail["none_installed"])
    return False, fail["separator"].join(failures), None, failures


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    stop = set(CONFIG["slug_stop_words"])
    return " ".join([w for w in words if w not in stop][:CONFIG["slug_words"]]) or CONFIG["slug_fallback"]


def save(task: str, text: str, engine: str, now: dt.datetime | None = None) -> str:
    """Write the deliverable as a vault note that passes the vault contract
    (frontmatter: name, description, tags, updated). Returns the path."""
    now = now or dt.datetime.now()
    os.makedirs(DRAFTS, exist_ok=True)
    name = CONFIG["draft_name"].format(now=now, slug=_slug(task))
    path = os.path.join(DRAFTS, f"{name}.md")
    desc = task.replace('"', "'").strip()
    body = CONFIG["draft_body"].format(
        name=re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
        who=_label(engine), short=desc[:CONFIG["draft_description_chars"]], desc=desc,
        now=now, text=text.strip())
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def _remember(task: str, ok: bool, engine, path, text: str) -> None:
    """One line in today's shared handoff note, tagged [denisse], so the main
    chat, the nightly review and the other agents see what she handed off --
    the way IRIS writes back in Atrium."""
    # Tests point DRAFTS at a temp folder; only a real hand-off reaches the shared log.
    if not os.path.abspath(DRAFTS).startswith(os.path.abspath(VAULT)):
        return
    try:
        from atrium import handoff
        n = CONFIG["handoff_task_chars"]
        if ok:
            line = CONFIG["handoff_ok"].format(task=task[:n], who=_label(engine),
                                               drafts=CONFIG["drafts_folder"],
                                               file=os.path.basename(path))
        else:
            line = CONFIG["handoff_fail"].format(task=task[:n], text=text[:CONFIG["handoff_text_chars"]])
        handoff.remember(CONFIG["handoff_agent"], [line])
    except Exception:
        pass


def start(task: str, context: list | None = None) -> str:
    """Hand the job off in the background. Returns what Denisse says now."""
    say = CONFIG["say"]
    drafts = CONFIG["drafts_folder"]
    if not any(shutil.which(spec["exe"]) for spec in CONFIG["engines"].values()):
        return say["not_installed"]
    if DRY_RUN:
        return say["dry_run"].format(drafts=drafts)

    def work():
        ok, text, engine, failures = run(task, context)
        path = None
        if ok:
            path = save(task, text, engine)
            msg = say["done"].format(who=_label(engine), task=task[:say["done_task_chars"]],
                                     drafts=drafts, file=os.path.basename(path))
            if failures:
                msg += say["fallback"].format(failed=_label(next(iter(CONFIG["engines"]))),
                                              why=failures[0][:say["fallback_chars"]],
                                              who=_label(engine))
        else:
            msg = say["failed"].format(text=text[:say["failed_chars"]])
        _remember(task, ok, engine, path if ok else None, text)
        if on_done:
            try:
                on_done(msg)
            except Exception:
                pass

    threading.Thread(target=work, daemon=True).start()
    return say["started"].format(drafts=drafts)
