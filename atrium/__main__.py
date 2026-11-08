"""python -m atrium <command>

  context [--lane LANE]  print the shared context pack (lanes and default: atrium/config.json)
  credits [--days N]     list decision lines in the vault that don't say who decided (exit 1 if any)
  snapshot [--out DIR]   write the Atrium Hub page's tables (default .review/hub)
  capture                Claude Code hook (stdin): save the session into the vault
  digest [--since HOURS] [--dry]
                         save what happened on this laptop since the last digest into the vault
  hub-save FILE          save the hub chat's digest (markdown FILE) into the vault
"""
import argparse
import sys

from . import conf, context, credits, snapshot


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m atrium", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("context")
    c.add_argument("--lane", default=conf.section("context")["default_lane"], choices=context.LANES)
    k = sub.add_parser("credits"); k.add_argument("--days", type=int, default=conf.section("credits")["default_days"])
    sn = sub.add_parser("snapshot"); sn.add_argument("--out", default=None)
    sub.add_parser("capture")
    dg = sub.add_parser("digest")
    dg.add_argument("--since", type=float, default=None, metavar="HOURS")
    dg.add_argument("--dry", action="store_true")
    hs = sub.add_parser("hub-save"); hs.add_argument("file")
    a = ap.parse_args(argv)
    if a.cmd == "context":
        sys.stdout.reconfigure(encoding="utf-8")
        print(context.show(a.lane))
        return 0
    if a.cmd == "capture":
        from . import capture
        return capture.main()
    if a.cmd == "digest":
        from . import digest
        sys.stdout.reconfigure(encoding="utf-8")
        print(digest.run(since_hours=a.since, dry=a.dry))
        return 0
    if a.cmd == "hub-save":
        from pathlib import Path
        from . import chatlog
        out = chatlog.save("hub", Path(a.file).read_text(encoding="utf-8"))
        print(f"saved {out['note']}" + (f" (not committed: {out['error']})" if out["error"] else ""))
        return 0 if out["committed"] else 1
    if a.cmd == "snapshot":
        from pathlib import Path
        print(snapshot.write(Path(a.out) if a.out else None))
        return 0
    bad = credits.check(a.days)
    for f, n, line in bad:
        print(f"{f}:{n}: {line}")
    print(conf.section("credits")["summary"].format(n=len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
