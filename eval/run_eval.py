"""
Run the prompt suite against the real Denisse brain and score it.

Two conditions, because the difference between them is exactly where the
2026-09-04 bug lived:

  clean    empty history -- Denisse at her best, the controlled baseline
  live     conversation.replay() -- what Chuen actually gets in the popup

A gap between the two means the stored history is dragging her down again.
That's the thing worth watching over a week, and it's why this runs both
rather than just the easy one.

Touches no real state: conversation history is never written, and the task
store is redirected to a scratch file for the run. An eval that pollutes
the thing it measures is worse than no eval.

    .venv\\Scripts\\python.exe eval\\run_eval.py [--out DIR] [--quiet]
    .venv\\Scripts\\python.exe eval\\run_eval.py --changed-only master   # quick check

A run takes the shared GPU lease first and does not start while another
local model job holds it (exit 3; --force overrides). The GPU's load is
sampled before the first case and a busy GPU marks the report noisy. Every
report ends with where the time went -- see eval/speed.py.
"""

import argparse
import contextlib
import datetime
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import speed  # noqa: E402
from eval.cases import CASES, LEAKS, MISHEARD, POSIX_LEAKS, ROBOTIC  # noqa: E402


def _tool_names() -> set:
    """Tool names with an underscore -- get_weather, current_track. Plain
    words like 'calculate' or 'recall' are ordinary English in a reply."""
    try:
        from brain import TOOL_DEFINITIONS
    except Exception:
        return set()
    return {d["function"]["name"] for d in TOOL_DEFINITIONS
            if "_" in d["function"]["name"]}


import re  # noqa: E402

# A Windows path in a reply, up to the first whitespace. Paths with spaces
# in them ("Passport ctc..pdf") are cut at the space, which is fine: the
# check below only asks whether that prefix appeared anywhere she could
# have got it from.
_WIN_PATH = re.compile(r"[A-Za-z]:\\[^\s\"'<>|*?`]+")


def _norm_path_text(s: str) -> str:
    # Tool output reaches the model JSON-escaped at times, so a real path
    # can show up with doubled backslashes on one side only.
    return re.sub(r"\\{2,}", r"\\", (s or "").lower())


def invented_paths(reply: str, sources: list) -> list:
    """Paths in the reply that came from nowhere.

    2026-10-28: asked why she couldn't find his passport, she replied with
    C:\\Users\\Xichu\\Documents\\Passport\\Passport.pdf and "Passport (2).pdf",
    offered to open one -- and neither the files nor the folder exist. No
    tool had returned them; she read a skill about organising files and
    made them up. The register calls invented results worse than any
    error, and until now nothing in the suite looked for them.

    A path is allowed if it appears in a tool result from this turn or in
    anything already in the conversation (scripted `before` turns, replayed
    history, his own message). Everything else is invented.
    """
    pool = _norm_path_text("\n".join(sources))
    bad = []
    for m in _WIN_PATH.finditer(reply or ""):
        p = m.group(0).rstrip(".,;:)]}")
        if len(p) > 3 and _norm_path_text(p) not in pool:
            bad.append(p)
    return bad


# Tools that change the real machine. The suite's rule was that a case must
# assert these are NOT called -- but an assertion only fails the case after
# the tool has already run. On 2026-10-29 the new latest_cv case opened a
# real CV on Chuen's screen mid-run. So they are vetoed here instead: the
# brain is told "User declined", nothing happens, and the case still
# judges what she tried to do. (RISKY_TOOLS are vetoed too, as before.)
EVAL_VETO = {
    "open_application", "run_shell_command", "write_file",
    "play_song", "pause_music", "resume_music", "skip_song", "previous_song",
    "clean_temp_files", "disable_startup_program", "restore_startup_program",
}


VAULT_EVAL_DIR = Path(
    r"C:\Users\Xichu\OneDrive - King's College London\Denisse\Denisse\Evals"
)
DATASET = ROOT / "eval" / "dataset.jsonl"


def check(case: dict, reply: str, tools: list, sources: list | None = None) -> list:
    """Return a list of failure strings; empty means the case passed.

    `sources` is everything she could legitimately have taken a file path
    from this turn. None skips the invented-path check (callers that don't
    have the transcript, e.g. older tooling)."""
    e = case["expect"]
    low = (reply or "").lower()
    fails = []

    if not (reply or "").strip() and not e.get("allow_empty"):
        fails.append("empty reply")

    if sources is not None:
        made_up = invented_paths(reply, sources)
        if made_up:
            fails.append(f"invented path(s) {made_up}")

    fails += _expect_fails(e, reply, tools)

    # Leaks are checked on every case, not only the ones that list them:
    # on 2026-10-20 the weather and news replies narrated their tool calls
    # and passed, and the news one went into dataset.jsonl.
    always = LEAKS + POSIX_LEAKS + ([] if case.get("misheard_ok") else MISHEARD)
    for s in dict.fromkeys(list(e.get("deny", [])) + always):
        if s.lower() in low:
            fails.append(f"contains {s!r}")

    if low.lstrip().startswith(("assistant", "user:", "system:")):
        fails.append("starts with a role name")

    named = sorted(n for n in _tool_names() if n in low)
    if named:
        fails.append(f"names tool {named}")

    return fails


def _expect_fails(e: dict, reply: str, tools: list) -> list:
    """The checks an `expect` dict names itself -- everything except `deny`
    and the always-on leak checks in check(). Split out so `any_of` can
    judge alternatives: "opens it, or asks one short question" is two
    different right answers."""
    low = (reply or "").lower()
    fails = []
    if e.get("no_tools") and tools:
        fails.append(f"called {tools} but should have called nothing")

    want = e.get("any_tool_of")
    if want and not (set(tools) & want):
        fails.append(f"called {tools or 'nothing'}, wanted one of {sorted(want)}")

    every = e.get("all_tools_of")
    if every and not every <= set(tools):
        fails.append(f"called {tools or 'nothing'}, never called {sorted(every - set(tools))}")

    banned = e.get("none_of")
    if banned and (set(tools) & banned):
        fails.append(f"called forbidden {sorted(set(tools) & banned)}")

    for s in e.get("must_contain", []):
        if s.lower() not in low:
            fails.append(f"missing {s!r}")

    if len((reply or "").split()) < e.get("min_words", 0):
        fails.append(f"under {e['min_words']} words")

    # in_order: these tools were all called, first calls in this order --
    # "searched, then opened", never an open on a guessed path first.
    order = e.get("in_order")
    if order:
        first = [tools.index(t) if t in tools else None for t in order]
        if None in first or first != sorted(first):
            fails.append(f"called {tools or 'nothing'}, wanted {order} in that order")

    if "max_words" in e and len((reply or "").split()) > e["max_words"]:
        fails.append(f"over {e['max_words']} words ({len((reply or '').split())})")

    anyof = e.get("contains_any")
    if anyof and not any(s.lower() in low for s in anyof):
        fails.append(f"missing any of {anyof!r}")

    # contains_each: several contains_any groups, and every group must
    # match -- "refuses" and "says why" are two separate things to find.
    for group in e.get("contains_each", []):
        if not any(s.lower() in low for s in group):
            fails.append(f"missing any of {group!r}")

    alts = e.get("any_of")
    if alts:
        tried = [_expect_fails(a, reply, tools) for a in alts]
        if all(tried):
            fails.append("no acceptable answer: " + " / ".join("; ".join(t) for t in tried))
    return fails


def robotic(reply: str) -> list:
    """Report-voice phrases in a reply. Counted, not failed -- see ROBOTIC."""
    low = (reply or "").lower()
    return [s for s in ROBOTIC if s in low]


def _fake_desktop(desk):
    """A case's `desktop` dict ({title, process, selection, clipboard})
    stands in for the real front window and clipboard, so "explain this"
    cases are repeatable and the nightly run never sends Ctrl+C. Cases
    without one get an empty desktop, not whatever is on screen at 3am."""
    import contextlib
    from unittest.mock import patch
    import desktop_context
    desk = desk or {}
    stack = contextlib.ExitStack()
    stack.enter_context(patch.object(desktop_context, "_w32_foreground",
                                     lambda: (desk["title"], 1) if desk.get("title") else None))
    stack.enter_context(patch.object(desktop_context, "_w32_process_name",
                                     lambda pid: desk.get("process", "")))
    stack.enter_context(patch.object(desktop_context, "selected_text",
                                     lambda *a, **k: desk.get("selection")))
    stack.enter_context(patch.object(desktop_context, "_w32_read_unicode",
                                     lambda: desk.get("clipboard")))
    return stack


FIXTURE_VAULT = ROOT / "eval" / "fixture_vault"


def _fixture_vault(which):
    """A case with `"vault": "fixture"` searches eval/fixture_vault -- tiny
    fake notes in the Atlas layout -- instead of his real vault, so the
    answer it checks is known and nothing of his is read. Every module that
    resolves a vault path is pointed there for the one case."""
    import contextlib
    from unittest.mock import patch
    stack = contextlib.ExitStack()
    if which != "fixture":
        return stack
    import planner
    import study_brief
    import vault_search
    v = str(FIXTURE_VAULT)
    stack.enter_context(patch.object(vault_search, "VAULT", v))
    stack.enter_context(patch.object(planner, "VAULT", v))
    stack.enter_context(patch.object(study_brief, "VAULT", v))
    real_cw = planner.coursework_deadlines
    stack.enter_context(patch.object(planner, "coursework_deadlines",
                                     lambda vault=v: real_cw(vault)))
    return stack


class _NoTkRoot:
    """Enough of a Tk root for StudyMode's clock: after() is recorded, never
    run, so the eval steps time by hand. The badge needs a real window and
    fails to build here, which StudyMode already treats as cosmetic."""

    def __init__(self):
        self.jobs = []

    def after(self, ms, fn):
        self.jobs.append(fn)
        return len(self.jobs)

    def after_cancel(self, job):
        pass


@contextlib.contextmanager
def headless_study(spec: dict):
    """A StudyMode with no window, registered as the process's one clock
    (study_mode._CURRENT) for the length of the case, so both the tray's
    whole-message fast path and the brain's study tools act on it. Started
    first when the case says it was running."""
    from unittest.mock import patch
    import study_mode

    with patch.object(study_mode, "_chime", lambda: None), \
            patch.object(study_mode, "_CURRENT", None), \
            patch.object(study_mode, "_dispatch", None):
        shown = []
        mode = study_mode.StudyMode(_NoTkRoot(), lambda *a, **k: shown.append(a[0]))
        if spec.get("active"):
            mode.start()
            if spec.get("paused"):
                mode.pause()
        yield mode


def study_turn(case: dict, mode=None):
    """Study mode's whole-message fast path: tray.py's chat matches the
    message against study_mode.match_command BEFORE the brain sees it, and
    runs the pomodoro clock itself. A case with a `study` dict goes down
    that same path, headless: {"active": was it running, "advance":
    [minutes, ...] to step the clock after the command}. Returns (reply,
    tools) when the fast path took the turn, None when the message falls
    through to the model -- which since 2026-11-03 has the study tools and
    acts on the same clock (`mode`). With no `mode` given, a headless clock
    is made for just this call (eval/test_fixture_cases.py).

    The reply is the clock's own status line after each step, so a case can
    assert what the tray menu would read; "Study mode off." only when stop
    left nothing behind (no phase, no pending tick, no badge)."""
    import datetime as _dt
    import study_mode

    if mode is None:
        with headless_study(case["study"]) as own:
            return study_turn(case, own)
    spec = case["study"]
    what = study_mode.match_command(case["prompt"])
    if what is None:
        return None
    lines = []
    if what == "study":
        mode.start()
        lines.append(f"{mode.status()} (round {mode.round_no})")
        for mins in spec.get("advance", []):
            mode.ends_at -= _dt.timedelta(minutes=mins)
            mode._check()
            lines.append(f"{mode.status()} (round {mode.round_no})")
    else:
        mode.stop()
        clean = not mode.active and mode._job is None and mode.badge is None
        lines.append("Study mode off." if clean else f"Study mode still running: {mode.status()}")
    return "\n".join(lines), [f"study_mode:{'start' if what == 'study' else 'stop'}"]


def run_case(brain, case: dict, history: list) -> dict:
    """One prompt through the real brain. Tool calls are allowed to run --
    they're read-only in this suite, and a mocked tool wouldn't tell us
    whether the real one still works."""
    if case.get("study") is None:
        return _run_case(brain, case, history, None)
    with headless_study(case["study"]) as mode:
        started = time.monotonic()
        handled = study_turn(case, mode)
        if handled is not None:
            reply, tools = handled
            fails = check(case, reply, tools, [reply])
            return {"id": case["id"], "prompt": case["prompt"],
                    "reply": " | ".join(reply.splitlines()), "tools": tools,
                    "seconds": round(time.monotonic() - started, 1),
                    "passed": not fails, "failures": fails,
                    "robotic": robotic(reply), "follow_up": True}
        # Fell through: the tray would hand it to the brain, so do the same,
        # with the clock still there for her study tools to act on.
        return _run_case(brain, case, history, mode)


def _run_case(brain, case: dict, history: list, mode) -> dict:
    import safety

    tools: list = []

    fixtures = case.get("fixtures") or {}

    def spy(name, args):
        tools.append(name)
        # A fixtured tool never really runs, so letting it through is safe:
        # that is how a case says "Spotify has no device" instead of the
        # veto's "user declined".
        if name in fixtures:
            return True
        return name not in safety.RISKY_TOOLS and name not in EVAL_VETO

    # `before` is a case's own scripted earlier turns, for follow-ups like
    # "and in Penang?" that only make sense after another question.
    messages = (list(history) + list(case.get("before", []))
                + [{"role": "user", "content": case["prompt"]}])
    # Snapshot before the call: respond() appends to the list it is given,
    # so reading `messages` afterwards would count her own reply as a
    # source and wave every invented path through.
    n_in = len(messages)
    sources = [m.get("content") or "" for m in messages]
    # Her system prompt states her home and project folders, so quoting
    # C:\Users\Xichu is knowledge, not invention. Without this the check
    # failed windows_paths and small_talk on 2026-10-29.
    import brain as _brain_mod
    sources.append(getattr(_brain_mod, "SYSTEM_PROMPT", "") or "")
    try:
        sources += [m.get("content") or "" for m in _brain_mod._style_examples()]
    except Exception:
        pass
    # `fixtures`: tool name -> the text that tool returns for this case,
    # whatever its arguments. For cases that need known data (a coursework
    # note, a failing search) or a tool that must not really run (a timer
    # would pop up on his screen 25 minutes into the night). Restored after.
    import tools as _tools_mod
    real ={n: _tools_mod.TOOL_FUNCTIONS.get(n) for n in fixtures}
    for n, text in fixtures.items():
        _tools_mod.TOOL_FUNCTIONS[n] = (lambda _t: (lambda **_kw: _t))(text)
    started = time.monotonic()
    out_msgs = []
    after = {}
    try:
        with _fake_desktop(case.get("desktop")), _fixture_vault(case.get("vault")), \
                _case_preferences(case.get("preferences"), after), \
                _case_tasks(case.get("tasks"), after):
            reply, out_msgs = brain.respond(messages, confirm=spy)
        error = None
    except Exception as exc:
        reply, error = "", f"{type(exc).__name__}: {exc}"
    finally:
        for n, fn in real.items():
            if fn is None:
                _tools_mod.TOOL_FUNCTIONS.pop(n, None)
            else:
                _tools_mod.TOOL_FUNCTIONS[n] = fn
    seconds = round(time.monotonic() - started, 1)

    # ...plus whatever a tool returned this turn.
    sources += [m.get("content") or "" for m in (out_msgs or [])[n_in:]
                if m.get("role") == "tool"]

    fails = [error] if error else check(case, reply, tools, sources)
    e = case["expect"]
    if e.get("preference_saved") and not error and not after.get("preference_added"):
        fails.append("no reply preference was saved")
    for word in ([] if error else e.get("tasks_gone", [])):
        if any(word.lower() in t for t in after.get("open_tasks", [])):
            fails.append(f"a task about {word!r} is still open")
    # A study case can say what state the clock must be left in.
    want = (case.get("study") or {}).get("ends")
    if want is not None and mode is not None and not error:
        state = "off" if not mode.active else ("paused" if getattr(mode, "paused", False) else "running")
        if state != want:
            fails.append(f"study clock left {state}, wanted {want}")
    return {
        "id": case["id"],
        "prompt": case["prompt"],
        "reply": " ".join((reply or "").split()),
        "tools": tools,
        "seconds": seconds,
        "passed": not fails,
        "failures": fails,
        "robotic": robotic(reply),
        "follow_up": bool(case.get("before")),
    }


@contextlib.contextmanager
def _case_preferences(seed, after: dict):
    """Every model case starts from its own reply preferences -- the ones it
    lists in `preferences` (his "from now on ..." lines), else none -- and
    the file is put back afterwards, so a preference one case saves can't
    make a later case pass or fail. `after["preference_added"]` says
    whether the turn saved one."""
    import reply_preferences as rp
    path = Path(rp.FILE)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    path.unlink(missing_ok=True)
    for text in seed or []:
        rp.add(text)
    n = len(rp.load())
    try:
        yield
    finally:
        after["preference_added"] = len(rp.load()) > n
        if old is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(old, encoding="utf-8")


@contextlib.contextmanager
def _case_tasks(seed, after: dict):
    """`tasks`: the case runs on its own task store -- the one known task
    plus these [text, due] pairs -- put back afterwards, so a reminder some
    other case added can't make "cancel the robotics one" ambiguous.
    Without the key the shared scratch store is used, as before.
    `after["open_tasks"]` is the open task texts when the turn ended."""
    if seed is None:
        yield
        return
    import tasks as tasks_mod
    path = Path(tasks_mod.TASKS_FILE)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    reseed_tasks(path)
    for text, due in seed:
        tasks_mod.add_task(text, due)
    try:
        yield
    finally:
        after["open_tasks"] = [t["text"].lower() for t in tasks_mod.list_tasks()]
        if old is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(old, encoding="utf-8")


def write_dataset(results: dict) -> int:
    """Append passing clean-condition exchanges to a curated JSONL set.

    This is the honest answer to "train Denisse": there is no usable
    training data yet (the whole stored history yielded 2 clean
    user->assistant pairs), and fine-tuning an 8B model needs more VRAM
    than this laptop has free anyway. So the harness *accumulates* the
    dataset a fine-tune would one day need, one verified-good exchange per
    night, instead of pretending to train on nothing.
    """
    DATASET.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if DATASET.exists():
        for line in DATASET.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["messages"][0]["content"])
            except Exception:
                continue

    added = 0
    with DATASET.open("a", encoding="utf-8") as f:
        for r in results["clean"]:
            if not r["passed"] or r["prompt"] in seen or not r["reply"]:
                continue
            # A follow-up's reply is meaningless without the turns before
            # it, and the dataset stores single user->assistant pairs.
            if r.get("follow_up"):
                continue
            f.write(json.dumps({
                "messages": [
                    {"role": "user", "content": r["prompt"]},
                    {"role": "assistant", "content": r["reply"]},
                ],
                "tools": r["tools"],
                "recorded": results["stamp"],
            }, ensure_ascii=False) + "\n")
            seen.add(r["prompt"])
            added += 1
    return added


def report(results: dict, added: int) -> str:
    n = len(results["clean"])
    subset = results.get("subset")
    lines = [
        "---", "tags: [denisse, eval]", f"updated: {results['date']}", "---", "",
        f"# Denisse eval — {results['stamp']}" + (" (subset)" if subset else ""), "",
        f"- **clean** (no history): {results['clean_pass']}/{n} passed",
        f"- **live** (real replayed history): {results['live_pass']}/{n} passed",
        f"- median reply: {results['median']}s",
        f"- sounds robotic: {results['robotic']} of {2 * n} replies",
        f"- curated dataset: +{added} this run, {results['dataset_total']} total",
    ]
    if subset:
        lines.append(f"- subset: {n} of {len(CASES)} cases, changed since `{subset['ref']}`:")
        lines += [f"    - {r}" for r in subset["reasons"]]
    if results.get("gpu"):
        lines += speed.gpu_lines(results["gpu"], results.get("lease"))
    lines.append("")

    gap = results["clean_pass"] - results["live_pass"]
    if gap > 0:
        lines += [
            f"> **{gap} case(s) pass clean but fail live.** The stored history is "
            "dragging her down — the same shape as the 2026-09-04 bug. Check "
            "what's accumulated in `memory/conversation.json`.", "",
        ]

    # What his real conversations say went wrong -- see learning.py. The
    # suite only ever asks its own fixed prompts; this is where new cases
    # for it come from.
    if results.get("learned"):
        lines += ["## Learned from live use", ""] + results["learned"] + [""]

    for cond in ("clean", "live"):
        lines += [f"## {cond}", ""]
        for r in results[cond]:
            mark = "PASS" if r["passed"] else "**FAIL**"
            lines.append(f"### {mark} `{r['id']}` — {r['seconds']}s")
            lines.append(f"> {r['prompt']}")
            lines.append("")
            lines.append(f"tools: `{r['tools'] or 'none'}`")
            lines.append("")
            lines.append(r["reply"][:400] or "_(empty)_")
            if r.get("robotic"):
                lines.append("")
                lines.append(f"_robotic: {r['robotic']}_")
            if r["failures"]:
                lines.append("")
                for f in r["failures"]:
                    lines.append(f"- {f}")
            lines.append("")
    if results.get("timing"):
        lines += speed.timing_lines(results["timing"])
    return "\n".join(lines)


def isolate_state() -> Path:
    """Point every store the suite can write at scratch files; return the
    scratch task file. Shared with demo/run_demo.py so the demo runs in the
    same sandbox as the nightly eval."""
    import tasks

    # The suite drives real tools, and the task cases really do add tasks.
    # Point the store at a scratch file first: before this, every nightly
    # run left two "call the dentist" tasks in Chuen's real list (one per
    # condition) and four had piled up by 2026-09-25. An eval that dirties
    # the thing it measures is worse than no eval.
    #
    # Seeded with one known task so the list case has something to find
    # regardless of what's really outstanding.
    scratch = Path(tempfile.mkdtemp(prefix="denisse-eval-")) / "tasks.json"
    tasks.TASKS_FILE = str(scratch)
    tasks.add_task("file the Spritzer paperwork", "friday")

    # Same for remembered facts. On 2026-10-18 a new recall case ("which
    # code editor do I use?") got a remember() call instead -- and it wrote
    # an invented code_editor fact into the REAL memory/facts.json, which
    # _save_facts then mirrored into the Obsidian vault. The suite now
    # reads a scratch copy of his real facts (so recall cases still test
    # real data) and the vault mirror is switched off for the run.
    import obsidian_sync
    import tools
    facts_scratch = scratch.with_name("facts.json")
    facts_scratch.write_text(json.dumps(tools._load_facts(), indent=2), encoding="utf-8")
    tools.FACTS_FILE = str(facts_scratch)
    obsidian_sync.sync_facts = lambda facts: None
    # The scratch task store still mirrored every save into the vault's
    # Tasks.md, so eval tasks ("file the Spritzer paperwork") could land in
    # his real notes. And a study plan the suite asks for is held in a
    # pending file -- keep that in scratch too. (2026-10-29)
    tasks._mirror_to_vault = lambda: None
    # The suite checks that she *decides* to hand a job to Claude; actually
    # spending his Claude allowance every night would be absurd.
    import delegate
    delegate.DRY_RUN = True
    import planner
    planner.PENDING_FILE = str(scratch.with_name("pending_plan.json"))
    # His "from now on ..." instructions (reply_preferences.py, 2026-11-03;
    # style_notes.jsonl before that): the suite's go to scratch, and the
    # suite reads none of his real ones, so a case can't pass on a
    # preference he happened to save.
    import reply_preferences
    reply_preferences.FILE = str(scratch.with_name("reply_preferences.json"))
    # A study brief is written into his vault's Drafts and its progress into
    # memory/jobs -- the suite's briefs go to scratch instead. (2026-10-30)
    import study_brief
    study_brief.DRAFTS = str(scratch.with_name("Drafts"))
    study_brief.JOBS_DIR = str(scratch.with_name("jobs"))
    return scratch


def reseed_tasks(scratch: Path) -> None:
    """A fresh scratch task store holding the one known task."""
    import tasks
    scratch.unlink(missing_ok=True)
    tasks.add_task("file the Spritzer paperwork", "friday")


_T0 = time.monotonic()  # process start, near enough: imports above are the bulk of startup


def add_gpu_args(ap: argparse.ArgumentParser) -> None:
    """The lease flags, shared with run_pass_k."""
    ap.add_argument("--wait-gpu", type=int, default=300, metavar="SECONDS",
                    help="how long to wait for the shared GPU lease (default 300)")
    ap.add_argument("--force", action="store_true",
                    help="run even if another local model job holds the GPU lease")


def main() -> int:
    ap = argparse.ArgumentParser(description="Score Denisse against the prompt suite")
    ap.add_argument("--out", default=None,
                    help=f"where to write the report (default: the vault's Evals folder; "
                         f"a temp folder for --changed-only)")
    ap.add_argument("--quiet", action="store_true", help="only print the summary line")
    ap.add_argument("--no-dataset", action="store_true",
                    help="don't append to dataset.jsonl -- for A/B runs")
    ap.add_argument("--changed-only", metavar="REF",
                    help="run only the cases a change since this git ref can reach, plus the "
                         "smoke set (eval/speed.py). For quick checks; full runs stay the A/B standard")
    ap.add_argument("--json", metavar="PATH", help="also write every result, timing and GPU reading here")
    ap.add_argument("--only", metavar="IDS", help="run only these case ids (comma-separated); a subset "
                    "run, so it curates nothing and skips the learning pass")
    ap.add_argument("--group", metavar="NAME", help="run only cases whose `group` is NAME or starts "
                    "with NAME: (e.g. chuen for every chuen:<area> case); a subset run like --only")
    add_gpu_args(ap)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    cases, subset = CASES, None
    if args.group:
        cases = in_group(CASES, args.group)
        if not cases:
            print(f"FATAL: no cases in group {args.group!r}")
            return 2
        subset = {"ref": "--group", "reasons": [f"group {args.group}: {len(cases)} cases"]}
    elif args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        known = {c["id"] for c in CASES}
        missing = [w for w in wanted if w not in known]
        if missing:
            print(f"FATAL: unknown case id(s): {', '.join(missing)}")
            return 2
        cases = [c for c in CASES if c["id"] in wanted]
        subset = {"ref": "--only", "reasons": [f"asked for: {', '.join(wanted)}"]}
    elif args.changed_only:
        try:
            cases, reasons = speed.select_cases(CASES, args.changed_only)
        except RuntimeError as exc:
            print(f"FATAL: {exc}")
            return 2
        subset = {"ref": args.changed_only, "reasons": reasons}
        print(f"--changed-only {args.changed_only}: {len(cases)} of {len(CASES)} cases")
        for r in reasons:
            print(f"  {r}")
    out_dir = Path(args.out) if args.out else (
        Path(tempfile.mkdtemp(prefix="denisse-eval-subset-")) if subset else VAULT_EVAL_DIR)

    lease = _gpu_lease(wait_s=args.wait_gpu, on_busy="run" if args.force else "exit")
    try:
        return _run(args, cases, subset, out_dir, lease)
    finally:
        if lease:
            lease.release()


def _run(args, cases, subset, out_dir, lease) -> int:
    import conversation

    scratch = isolate_state()
    prof = speed.Profile()
    prof.install()

    now = datetime.datetime.now()
    results = {"stamp": now.strftime("%Y-%m-%d %H:%M"), "date": now.strftime("%Y-%m-%d"),
               "subset": subset, "lease": lease_info(lease)}

    try:
        brain = conversation.get_brain()
    except Exception as exc:
        print(f"FATAL: brain unavailable — {exc}")
        return 2

    # Sampled after the brain is up and before the first case: this is the
    # GPU the cases are about to share. A noisy start is flagged in the
    # report, not fatal -- the pass/fail still counts, the seconds don't.
    results["gpu"] = speed.gpu_snapshot()
    if results["gpu"]["noisy"]:
        print(f"note: GPU already {results['gpu']['util_pct']}% busy -- timings in this run are noisy")
    startup_s = time.monotonic() - _T0
    cases_t0 = time.monotonic()

    conditions = {"clean": [], "live": conversation.replay()}
    for cond, history in conditions.items():
        # Fresh task store per condition. Until 2026-10-21 it was seeded
        # once, so clean's task_complete_one closed the Spritzer task and
        # live's had nothing to close -- it was testing nothing.
        if cond != "clean":
            reseed_tasks(scratch)
        results[cond] = []
        for case in cases:
            before = prof.snapshot()
            r = run_case(brain, case, history)
            r["profile"] = speed.Profile.delta(before, prof.snapshot())
            results[cond].append(r)
            if not args.quiet:
                print(f"[{'OK  ' if r['passed'] else 'FAIL'}] {cond:5} {r['id']:15} "
                      f"{r['seconds']:5}s  {r['failures'] or ''}")
    cases_s = time.monotonic() - cases_t0

    n = len(cases)
    results["clean_pass"] = sum(r["passed"] for r in results["clean"])
    results["live_pass"] = sum(r["passed"] for r in results["live"])
    times = sorted(r["seconds"] for c in ("clean", "live") for r in results[c])
    results["median"] = times[len(times) // 2] if times else 0
    results["robotic"] = sum(bool(r["robotic"]) for c in ("clean", "live") for r in results[c])

    # A subset run is a quick check: it curates nothing and skips the
    # learning pass, which is the nightly's job.
    added = 0 if (args.no_dataset or subset) else write_dataset(results)

    learning_t0 = time.monotonic()
    if not subset:
        # Never let it cost the night's eval: a bad history file is
        # reported, not fatal.
        try:
            import learning
            results["learned"] = learning.summary(learning.run())
        except Exception as exc:
            results["learned"] = [f"- learning pass failed: {exc}"]
    results["dataset_total"] = sum(1 for _ in DATASET.open(encoding="utf-8")) if DATASET.exists() else 0
    results["timing"] = prof.summary(wall_s=time.monotonic() - _T0, cases_s=cases_s,
                                     startup_s=startup_s, learning_s=time.monotonic() - learning_t0)

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{results['date']}.md").write_text(
            report(results, added), encoding="utf-8"
        )
        where = str(out_dir / f"{results['date']}.md")
    except OSError as exc:
        where = f"(could not write report: {exc})"
    if args.json:
        try:
            Path(args.json).write_text(json.dumps(results, indent=1, ensure_ascii=False, default=str),
                                       encoding="utf-8")
        except OSError as exc:
            print(f"(could not write json: {exc})")

    t = results["timing"]
    gpu = results["gpu"]
    print(f"\n{results['stamp']}  clean {results['clean_pass']}/{n}  "
          f"live {results['live_pass']}/{n}  "
          f"median {results['median']}s  robotic {results['robotic']}  dataset {results['dataset_total']}")
    print(f"wall {t['wall_s']}s: startup {t['startup_s']}  model {t['model_s']} "
          f"(prefill {t['prefill_s']}, generation {t['gen_s']})  tools {t['tool_s']}  "
          f"python {t['other_s']}  learning {t['learning_s']}"
          + (f"  | GPU at start {gpu['util_pct']}%" if gpu.get("util_pct") is not None else "")
          + ("  NOISY" if gpu.get("noisy") else ""))
    for line in group_lines(results, cases):
        print(line)
    print(where)
    return 0 if results["live_pass"] == n else 1


def in_group(cases: list, name: str) -> list:
    """Cases whose `group` is `name` or sits under it (`name:<area>`)."""
    return [c for c in cases if c.get("group") == name or (c.get("group") or "").startswith(name + ":")]


def group_lines(results: dict, cases: list) -> list:
    """Pass counts per `group`, per condition -- "" when no case has one."""
    groups = list(dict.fromkeys(c["group"] for c in cases if c.get("group")))
    out = []
    for g in groups:
        ids = {c["id"] for c in cases if c.get("group") == g}
        cells = []
        for cond in ("clean", "live"):
            rs = [r for r in results.get(cond, []) if r["id"] in ids]
            cells.append(f"{cond} {sum(r['passed'] for r in rs)}/{len(rs)}")
        out.append(f"  {g:22} " + "  ".join(cells))
    return out


def lease_info(lease) -> dict:
    if lease is None:
        return {"held": False, "holder": _LAST_HOLDER}
    return {"held": True, "holder": lease.holder}


_LAST_HOLDER = None


def _gpu_lease(wait_s: int = 300, on_busy: str = "run"):
    """Take the shared local-inference lease (.review/gpu.lease in the main
    checkout) so Orchestra's local team doesn't start a model call mid-eval,
    and so the eval doesn't start mid-call. Waits `wait_s` for the holder to
    finish. Still held after that:

        on_busy="exit"  print who holds it and exit 3 (the scheduled run's
                        own "skipped, not a run" code). The eval's default:
                        a round timed against someone else's model job is
                        the noise this exists to stop.
        on_busy="run"   note it and run anyway (--force; the bakeoff's old
                        behaviour, kept for it).
    """
    global _LAST_HOLDER
    try:
        from orchestra.local_team.lease import GpuLease
    except Exception:
        return None
    lease = GpuLease.for_repo(ROOT, "eval/run_eval.py", ttl_s=3 * 3600)
    if not lease.acquire(wait_s=wait_s, poll_s=5):
        held = lease.info() or {}
        _LAST_HOLDER = held.get("holder", "?")
        if on_busy == "exit":
            print(f"GPU busy: lease held by {_LAST_HOLDER} (pid {held.get('pid', '?')}) after "
                  f"waiting {wait_s}s. Not running -- --wait-gpu N to wait longer, --force to run anyway.")
            raise SystemExit(3)
        print(f"note: GPU lease still held by {_LAST_HOLDER}; running anyway")
        return None
    return lease


if __name__ == "__main__":
    raise SystemExit(main())
