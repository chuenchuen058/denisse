"""
Run chosen eval cases k times in both conditions and report pass^k.

pass^k = the case passed in ALL k rounds -- the reliability number, not the
best-of. Same sandbox as run_eval (isolate_state), and nothing is written to
dataset.jsonl or the vault: the table is printed, and --md writes it too.

    .venv\\Scripts\\python.exe eval\\run_pass_k.py --rounds 3 --prefix vault_ --prefix study_
    .venv\\Scripts\\python.exe eval\\run_pass_k.py --id vault_lecturer --rounds 5
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import run_eval  # noqa: E402
from eval.cases import CASES  # noqa: E402


def pick(ids, prefixes):
    return [c for c in CASES
            if c["id"] in ids or any(c["id"].startswith(p) for p in prefixes)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--id", action="append", default=[], help="case id (repeatable)")
    ap.add_argument("--prefix", action="append", default=[], help="case id prefix (repeatable)")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--json", help="write every result here")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    cases = pick(set(args.id), args.prefix)
    if not cases:
        print("no cases matched")
        return 2
    import conversation
    scratch = run_eval.isolate_state()
    brain = conversation.get_brain()
    conditions = {"clean": [], "live": conversation.replay()}
    results = {c["id"]: {cond: [] for cond in conditions} for c in cases}
    for rnd in range(1, args.rounds + 1):
        for cond, history in conditions.items():
            run_eval.reseed_tasks(scratch)
            for case in cases:
                r = run_eval.run_case(brain, case, history)
                r["round"] = rnd
                results[case["id"]][cond].append(r)
                print(f"[{'OK  ' if r['passed'] else 'FAIL'}] r{rnd} {cond:5} {r['id']:26} "
                      f"{r['seconds']:5}s  {r['tools']}  {r['failures'] or ''}", flush=True)

    print(f"\n| case | clean pass^{args.rounds} | live pass^{args.rounds} |\n|---|---|---|")
    for cid, by in results.items():
        cells = []
        for cond in conditions:
            n = sum(r["passed"] for r in by[cond])
            cells.append(f"{'yes' if n == args.rounds else 'no'} ({n}/{args.rounds})")
        print(f"| `{cid}` | {cells[0]} | {cells[1]} |")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0 if all(r["passed"] for by in results.values() for rs in by.values() for r in rs) else 1


if __name__ == "__main__":
    _lease = run_eval._gpu_lease()
    try:
        rc = main()
    finally:
        if _lease:
            _lease.release()
    raise SystemExit(rc)
