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
"""

import argparse
import datetime
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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

    if e.get("no_tools") and tools:
        fails.append(f"called {tools} but should have called nothing")

    want = e.get("any_tool_of")
    if want and not (set(tools) & want):
        fails.append(f"called {tools or 'nothing'}, wanted one of {sorted(want)}")

    banned = e.get("none_of")
    if banned and (set(tools) & banned):
        fails.append(f"called forbidden {sorted(set(tools) & banned)}")

    for s in e.get("must_contain", []):
        if s.lower() not in low:
            fails.append(f"missing {s!r}")

    if len((reply or "").split()) < e.get("min_words", 0):
        fails.append(f"under {e['min_words']} words")

    anyof = e.get("contains_any")
    if anyof and not any(s.lower() in low for s in anyof):
        fails.append(f"missing any of {anyof!r}")

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


def robotic(reply: str) -> list:
    """Report-voice phrases in a reply. Counted, not failed -- see ROBOTIC."""
    low = (reply or "").lower()
    return [s for s in ROBOTIC if s in low]


def run_case(brain, case: dict, history: list) -> dict:
    """One prompt through the real brain. Tool calls are allowed to run --
    they're read-only in this suite, and a mocked tool wouldn't tell us
    whether the real one still works."""
    import safety

    tools: list = []

    def spy(name, args):
        tools.append(name)
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
    started = time.monotonic()
    out_msgs = []
    try:
        reply, out_msgs = brain.respond(messages, confirm=spy)
        error = None
    except Exception as exc:
        reply, error = "", f"{type(exc).__name__}: {exc}"
    seconds = round(time.monotonic() - started, 1)

    # ...plus whatever a tool returned this turn.
    sources += [m.get("content") or "" for m in (out_msgs or [])[n_in:]
                if m.get("role") == "tool"]

    fails = [error] if error else check(case, reply, tools, sources)
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
    lines = [
        "---", "tags: [denisse, eval]", f"updated: {results['date']}", "---", "",
        f"# Denisse eval — {results['stamp']}", "",
        f"- **clean** (no history): {results['clean_pass']}/{len(CASES)} passed",
        f"- **live** (real replayed history): {results['live_pass']}/{len(CASES)} passed",
        f"- median reply: {results['median']}s",
        f"- sounds robotic: {results['robotic']} of {2 * len(CASES)} replies",
        f"- curated dataset: +{added} this run, {results['dataset_total']} total",
        "",
    ]

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
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Score Denisse against the prompt suite")
    ap.add_argument("--out", default=str(VAULT_EVAL_DIR), help="where to write the report")
    ap.add_argument("--quiet", action="store_true", help="only print the summary line")
    ap.add_argument("--no-dataset", action="store_true",
                    help="don't append to dataset.jsonl -- for A/B runs")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    import conversation
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
    # A study brief is written into his vault's Drafts and its progress into
    # memory/jobs -- the suite's briefs go to scratch instead. (2026-10-30)
    import study_brief
    study_brief.DRAFTS = str(scratch.with_name("Drafts"))
    study_brief.JOBS_DIR = str(scratch.with_name("jobs"))

    now = datetime.datetime.now()
    results = {"stamp": now.strftime("%Y-%m-%d %H:%M"), "date": now.strftime("%Y-%m-%d")}

    try:
        brain = conversation.get_brain()
    except Exception as exc:
        print(f"FATAL: brain unavailable — {exc}")
        return 2

    conditions = {"clean": [], "live": conversation.replay()}
    for cond, history in conditions.items():
        # Fresh task store per condition. Until 2026-10-21 it was seeded
        # once, so clean's task_complete_one closed the Spritzer task and
        # live's had nothing to close -- it was testing nothing.
        if cond != "clean":
            scratch.unlink(missing_ok=True)
            tasks.add_task("file the Spritzer paperwork", "friday")
        results[cond] = []
        for case in CASES:
            r = run_case(brain, case, history)
            results[cond].append(r)
            if not args.quiet:
                print(f"[{'OK  ' if r['passed'] else 'FAIL'}] {cond:5} {r['id']:15} "
                      f"{r['seconds']:5}s  {r['failures'] or ''}")

    results["clean_pass"] = sum(r["passed"] for r in results["clean"])
    results["live_pass"] = sum(r["passed"] for r in results["live"])
    times = sorted(r["seconds"] for c in ("clean", "live") for r in results[c])
    results["median"] = times[len(times) // 2] if times else 0
    results["robotic"] = sum(bool(r["robotic"]) for c in ("clean", "live") for r in results[c])

    added = 0 if args.no_dataset else write_dataset(results)

    # Never let it cost the night's eval: a bad history file is reported,
    # not fatal.
    try:
        import learning
        results["learned"] = learning.summary(learning.run())
    except Exception as exc:
        results["learned"] = [f"- learning pass failed: {exc}"]
    results["dataset_total"] = sum(1 for _ in DATASET.open(encoding="utf-8")) if DATASET.exists() else 0

    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{results['date']}.md").write_text(
            report(results, added), encoding="utf-8"
        )
        where = str(out_dir / f"{results['date']}.md")
    except OSError as exc:
        where = f"(could not write report: {exc})"

    print(f"\n{results['stamp']}  clean {results['clean_pass']}/{len(CASES)}  "
          f"live {results['live_pass']}/{len(CASES)}  "
          f"median {results['median']}s  robotic {results['robotic']}  dataset {results['dataset_total']}")
    print(where)
    return 0 if results["live_pass"] == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
