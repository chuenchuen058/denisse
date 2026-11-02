"""python -m atrium <command>

  context [--lane claude|codex|gemini|copilot|denisse]   print the shared context pack
  credits [--days 7]     list decision lines in the vault that don't say who decided (exit 1 if any)
  snapshot [--out DIR]   write the Atrium Hub page's tables (default .review/hub)
  capture                Claude Code hook (stdin): save the session into the vault
"""
import argparse
import sys

from . import context, credits, snapshot


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m atrium", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("context"); c.add_argument("--lane", default="claude", choices=context.LANES)
    k = sub.add_parser("credits"); k.add_argument("--days", type=int, default=7)
    sn = sub.add_parser("snapshot"); sn.add_argument("--out", default=None)
    sub.add_parser("capture")
    a = ap.parse_args(argv)
    if a.cmd == "context":
        sys.stdout.reconfigure(encoding="utf-8")
        print(context.show(a.lane))
        return 0
    if a.cmd == "capture":
        from . import capture
        return capture.main()
    if a.cmd == "snapshot":
        from pathlib import Path
        print(snapshot.write(Path(a.out) if a.out else None))
        return 0
    bad = credits.check(a.days)
    for f, n, line in bad:
        print(f"{f}:{n}: {line}")
    print(f"{len(bad)} decision line(s) without a credit")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
