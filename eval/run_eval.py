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

from eval.cases import CASES  # noqa: E402


VAULT_EVAL_DIR = Path(
    r"C:\Users\Xichu\OneDrive - King's College London\Denisse\Denisse\Evals"
)
DATASET = ROOT / "eval" / "dataset.jsonl"


def check(case: dict, reply: str, tools: list) -> list:
    """Return a list of failure strings; empty means the case passed."""
    e = case["expect"]
    low = (reply or "").lower()
    fails = []

    if not (reply or "").strip():
        fails.append("empty reply")

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

    for s in e.get("deny", []):
        if s.lower() in low:
            fails.append(f"contains {s!r}")

    return fails


def run_case(brain, case: dict, history: list) -> dict:
    """One prompt through the real brain. Tool calls are allowed to run --
    they're read-only in this suite, and a mocked tool wouldn't tell us
    whether the real one still works."""
    import safety

    tools: list = []

    def spy(name, args):
        tools.append(name)
        return name not in safety.RISKY_TOOLS

    messages = list(history) + [{"role": "user", "content": case["prompt"]}]
    started = time.monotonic()
    try:
        reply, _ = brain.respond(messages, confirm=spy)
        error = None
    except Exception as exc:
        reply, error = "", f"{type(exc).__name__}: {exc}"
    seconds = round(time.monotonic() - started, 1)

    fails = [error] if error else check(case, reply, tools)
    return {
        "id": case["id"],
        "prompt": case["prompt"],
        "reply": " ".join((reply or "").split()),
        "tools": tools,
        "seconds": seconds,
        "passed": not fails,
        "failures": fails,
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

    now = datetime.datetime.now()
    results = {"stamp": now.strftime("%Y-%m-%d %H:%M"), "date": now.strftime("%Y-%m-%d")}

    try:
        brain = conversation.get_brain()
    except Exception as exc:
        print(f"FATAL: brain unavailable — {exc}")
        return 2

    conditions = {"clean": [], "live": conversation.replay()}
    for cond, history in conditions.items():
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

    added = write_dataset(results)
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
          f"median {results['median']}s  dataset {results['dataset_total']}")
    print(where)
    return 0 if results["live_pass"] == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
