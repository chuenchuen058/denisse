"""Detached tutor worker. Cards and Anki only run with explicit config opt-ins."""
import argparse
from pathlib import Path
import subprocess
import sys

from . import conf


def commands(config, vault, modules):
    c = config["ingest"]
    values = {"python": sys.executable, "vault": str(vault)}
    yield [part.format(**values) for part in c["command"]]
    if c["cards"]:
        for module in modules:
            yield [part.format(**values, module=module) for part in c["cards_command"]]
    if c["anki_sync"]:
        yield [part.format(**values) for part in c["anki_command"]]


def main(argv=None):
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--modules", required=True)
    args = parser.parse_args(argv)
    for command in commands(conf.load(), args.vault, json.loads(args.modules)):
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1])
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
