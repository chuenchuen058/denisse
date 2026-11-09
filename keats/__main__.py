"""python -m keats login | pull [--dry] [--ingest]."""
import argparse
import sys

from . import conf
from .browser import session
from .pull import exclusive, run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login")
    pull = commands.add_parser("pull")
    pull.add_argument("--dry", action="store_true")
    pull.add_argument("--ingest", action="store_true")
    pull.add_argument("--unattended", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    config = conf.load()
    try:
        if args.command == "login":
            with exclusive(conf.profile(config), config), session(config, login=True):
                print(config["output"]["login_ok"])
        else:
            run(config, conf.vault(), dry=args.dry, ingest=args.ingest, unattended=args.unattended)
        return 0
    except Exception as exc:
        print(config["output"]["error"].format(error=exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
