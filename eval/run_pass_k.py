"""
Run chosen eval cases k times in both conditions and report pass^k.

pass^k = the case passed in ALL k rounds -- the reliability number, not the
best-of. Same sandbox as run_eval (isolate_state), and nothing is written to
dataset.jsonl or the vault: the table is printed, and --md writes it too.

    .venv\\Scripts\\python.exe eval\\run_pass_k.py --rounds 3 --prefix vault_ --prefix study_
    .venv\\Scripts\\python.exe eval\\run_pass_k.py --id vault_lecturer --rounds 5
    .venv\\Scripts\\python.exe eval\\run_pass_k.py --rounds 3 --changed-only master
    .venv\\Scripts\\python.exe eval\\run_pass_k.py --rounds 3 --all

Takes the shared GPU lease like run_eval does, and does not start while
another local model job holds it (exit 3; --force overrides).
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import run_eval, speed  # noqa: E402
from eval.cases import CASES  # noqa: E402


def pick(ids, prefixes):
    return [c for c in CASES
            if c["id"] in ids or any(c["id"].startswith(p) for p in prefixes)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--id", action="append", default=[], help="case id (repeatable)")
    ap.add_argument("--prefix", action="append", default=[], help="case id prefix (repeatable)")
    ap.add_argument("--all", action="store_true", help="every case")
    ap.add_argument("--changed-only", metavar="REF",
                    help="the cases a change since this git ref can reach, plus the smoke set")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--json", help="write every result here")
    run_eval.add_gpu_args(ap)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    cases = list(CASES) if args.all else pick(set(args.id), args.prefix)
    if args.changed_only:
        chosen, reasons = speed.select_cases(CASES, args.changed_only)
        print(f"--changed-only {args.changed_only}: {len(chosen)} of {len(CASES)} cases")
        for r in reasons:
            print(f"  {r}")
        have = {c["id"] for c in cases}
        cases += [c for c in chosen if c["id"] not in have]
        cases = [c for c in CASES if c in cases]  # suite order
    if not cases:
        print("no cases matched")
        return 2
    lease = run_eval._gpu_lease(wait_s=args.wait_gpu, on_busy="run" if args.force else "exit")
    try:
        return _run(args, cases)
    finally:
        if lease:
            lease.release()


def _run(args, cases) -> int:
    import conversation
    scratch = run_eval.isolate_state()
    prof = speed.Profile()
    prof.install()
    brain = conversation.get_brain()
    gpu = speed.gpu_snapshot()
    if gpu["noisy"]:
        print(f"note: GPU already {gpu['util_pct']}% busy -- timings in this run are noisy")
    conditions = {"clean": [], "live": conversation.replay()}
    results = {c["id"]: {cond: [] for cond in conditions} for c in cases}
    round_s = []
    for rnd in range(1, args.rounds + 1):
        t0 = time.monotonic()
        for cond, history in conditions.items():
            run_eval.reseed_tasks(scratch)
            for case in cases:
                before = prof.snapshot()
                r = run_eval.run_case(brain, case, history)
                r["round"] = rnd
                r["profile"] = speed.Profile.delta(before, prof.snapshot())
                results[case["id"]][cond].append(r)
                print(f"[{'OK  ' if r['passed'] else 'FAIL'}] r{rnd} {cond:5} {r['id']:26} "
                      f"{r['seconds']:5}s  {r['tools']}  {r['failures'] or ''}", flush=True)
        round_s.append(round(time.monotonic() - t0, 1))

    print(f"\n| case | clean pass^{args.rounds} | live pass^{args.rounds} |\n|---|---|---|")
    for cid, by in results.items():
        cells = []
        for cond in conditions:
            n = sum(r["passed"] for r in by[cond])
            cells.append(f"{'yes' if n == args.rounds else 'no'} ({n}/{args.rounds})")
        print(f"| `{cid}` | {cells[0]} | {cells[1]} |")
    t = prof.summary(wall_s=sum(round_s), cases_s=sum(round_s), startup_s=0)
    print(f"\nrounds: {', '.join(f'{s}s' for s in round_s)}  model {t['model_s']}s "
          f"(prefill {t['prefill_s']}, generation {t['gen_s']})  tools {t['tool_s']}s  "
          f"python {t['other_s']}s"
          + (f"  | GPU at start {gpu['util_pct']}%" if gpu.get("util_pct") is not None else "")
          + ("  NOISY" if gpu.get("noisy") else ""))
    if args.json:
        Path(args.json).write_text(json.dumps({"cases": results, "rounds_s": round_s, "timing": t, "gpu": gpu},
                                              indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return 0 if all(r["passed"] for by in results.values() for rs in by.values() for r in rs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
