import argparse
import json

from .core import build_snapshot_data, collect_alerts, snapshot, weekly_note


def _snapshot_cli(args):
    data = build_snapshot_data()
    if args.dry_run:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    path = snapshot(dry_run=False)
    print(path)
    return 0


def _alerts_cli(args):
    alerts = collect_alerts(build_snapshot_data(), dry_run=args.dry_run)
    print(json.dumps({"alerts": alerts}, ensure_ascii=False, indent=2))
    return 0


def _weekly_cli(args):
    out = weekly_note(dry_run=args.dry_run)
    print(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m markets", description="Market snapshot, alerts, and weekly summary.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot", help="Write .review/hub/markets.json")
    snapshot_parser.add_argument("--dry-run", action="store_true", help="Print JSON to stdout without writing files")
    snapshot_parser.set_defaults(func=_snapshot_cli)

    alerts_parser = subparsers.add_parser("alerts", help="Check market moves against threshold and dedupe by date")
    alerts_parser.add_argument("--dry-run", "--dry_run", action="store_true", dest="dry_run",
                               help="Only print alerts without persisting dedupe state")
    alerts_parser.set_defaults(func=_alerts_cli)

    weekly_parser = subparsers.add_parser("weekly", help="Render the weekly vault note")
    weekly_parser.add_argument("--dry-run", action="store_true", help="Print the weekly note to stdout without writing it")
    weekly_parser.set_defaults(func=_weekly_cli)

    ns = parser.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())
