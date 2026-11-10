"""python -m bench run --models llama3.1:8b,qwen3.5:9b --jobs all [--date YYYY-MM-DD] [--fresh]
python -m bench pick [--file .review/bench/<date>.json]
python -m bench jobs

run: every (model, job) cell on the bench fixtures, saved to .review/bench/<date>.json and
.md after each one. Rerun the same command to resume. Exit 0 all done, 1 some cell skipped or
errored, 2 bad arguments, 3 GPU lease busy, 4 on battery.
pick: proposes a model per job from the latest results. It never edits model_routing.json.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench import core  # noqa: E402


def main(argv: list | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    cfg = core.load_config()
    ap = argparse.ArgumentParser(prog="python -m bench", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="score every model on every job")
    r.add_argument("--models", default=",".join(cfg["models"]), help="comma-separated installed Ollama models")
    r.add_argument("--jobs", default="all", help="'all' or comma-separated job names (python -m bench jobs)")
    r.add_argument("--date", default=None, help="results file to write or resume (default today)")
    r.add_argument("--fresh", action="store_true", help="start the date's file over instead of resuming")
    p = sub.add_parser("pick", help="propose a model per job from the latest results")
    p.add_argument("--file", default=None, help="a results file (default: the latest in out_dir)")
    sub.add_parser("jobs", help="list the jobs")
    args = ap.parse_args(argv)

    if args.cmd == "jobs":
        import model_routing
        what = model_routing.jobs()
        for name in core.job_names(cfg, "all"):
            print(f"{name:22} {(what.get(name) or {}).get('what', '')}")
        return 0
    if args.cmd == "pick":
        path = Path(args.file) if args.file else core.latest(cfg)
        if path is None or not path.exists():
            print(f"no results yet in {core.out_dir(cfg)}: run `python -m bench run` first")
            return 2
        rows = core.pick(core.load_results(path), cfg)
        print("\n".join(core.pick_lines(rows, cfg, path)))
        return 0
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    try:
        names = core.job_names(cfg, args.jobs)
    except ValueError as exc:
        print(exc)
        return core.EXIT["usage"]
    if not models:
        print("no models given")
        return core.EXIT["usage"]
    code = core.run(models, names, cfg=cfg, date=args.date, fresh=args.fresh)
    print(core.results_path(cfg, args.date or core.dt.date.today().isoformat()).with_suffix(".md"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
