"""Run the eval prompt suite (eval/cases.py) for N rounds, clean condition only.

Worker for demo/run_demo.py, run in its own process. Same sandbox as the
nightly eval (eval.run_eval.isolate_state): scratch tasks, facts, plans and
notes; machine-changing tools vetoed; desktop faked. The live condition is
not run, because it replays Chuen's real conversation history.

Writes one JSON list of per-attempt records. Reply text is NOT written: the
recall cases answer from his real facts, and the results are committed to a
public repo. Only ids, pass/fail, tools called, timings and failure reasons
(with any file paths redacted) are kept.

    .venv\\Scripts\\python.exe demo\\suite_rounds.py --rounds 3 --out logs\\suite.json
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))

from evidence import redact  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", nargs="*", help="case ids to run (default: all)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from eval import run_eval
    from eval.cases import CASES
    import conversation

    scratch = run_eval.isolate_state()
    brain = conversation.get_brain()
    cases = [c for c in CASES if not args.only or c["id"] in args.only]
    records = []
    for rnd in range(1, args.rounds + 1):
        run_eval.reseed_tasks(scratch)
        for case in cases:
            r = run_eval.run_case(brain, case, [])
            rec = {"round": rnd, "id": r["id"], "passed": r["passed"],
                   "tools": r["tools"], "seconds": r["seconds"],
                   "failures": [redact(f) for f in r["failures"]],
                   "robotic": bool(r["robotic"])}
            records.append(rec)
            print(json.dumps(rec), flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"PASS {sum(r['passed'] for r in records)}/{len(records)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
