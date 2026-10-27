"""Inspect supported preferences: python scripts/brain_settings.py [--init]."""
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain_rules
import instincts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="create private defaults only if the file is absent")
    args = parser.parse_args()
    if args.init:
        path = brain_rules.rules_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as f:
                json.dump(asdict(brain_rules.Rules()), f, indent=2)
                f.write("\n")
        except FileExistsError:
            pass
    print(json.dumps({"rules": brain_rules.status(), "learning": instincts.status()}, indent=2))
    return 0 if not brain_rules.status()["error"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
