"""Configure and inspect Denisse's own standing instructions (local, private)."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import standing_instructions as standing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="create default instructions, never overwrite")
    parser.add_argument("--enable", action="store_true", help="enable independent work")
    parser.add_argument("--pause", action="store_true", help="pause all standing jobs")
    parser.add_argument("--job", choices=sorted(standing.JOBS), help="target one job with enable/pause")
    parser.add_argument("--run", choices=sorted(standing.JOBS), help="run a due enabled job once")
    args = parser.parse_args()
    if args.enable and args.pause:
        parser.error("choose enable or pause")
    if args.job and not (args.enable or args.pause):
        parser.error("--job requires --enable or --pause")
    engine = standing.Engine()
    if args.init:
        engine.config.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation: never replace instructions from another session.
        try:
            with engine.config.open("x", encoding="utf-8") as target:
                json.dump(standing.defaults(), target, indent=2)
        except FileExistsError:
            print("Existing instruction file retained.")
    try:
        if args.enable or args.pause:
            engine.set_enabled(args.enable, args.job)
        if args.run:
            outcome = engine.run_job(args.run)
            print(json.dumps(outcome or {"status": "skipped", "reason": "Paused, cooling down, locked or invalid state; inspect status."}, indent=2))
            return 1 if outcome and outcome["status"] == "failed" else 0
        status = engine.status()
        print(json.dumps({"settings_file": str(engine.config), **status}, indent=2))
        return 1 if status["error"] else 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
