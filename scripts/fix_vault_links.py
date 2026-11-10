"""
One-off: repair broken wikilinks in the vault.

Two causes, both found by scripts/vault_link_audit.py on 2026-09-05:

1. Renames that weren't followed through. `memory_bank_vault` became
   `vault_setup` and `denisse_vault` became `README` during the portability
   rebuild, but wikilinks pointing at the old names were left behind. In
   Obsidian's graph view each one draws a hollow "phantom" node.

2. Wikilinks hard-wrapped across a line break, e.g.

       [[Hey
       Denisse Wake Word]]

   A newline inside the brackets breaks the link -- Obsidian stops
   resolving it. These are in the Project Log notes and predate this
   session; most likely the Linter plugin re-wrapping paragraphs at a
   fixed width without knowing not to split a wikilink.

Idempotent. Prints what it would change; writes only with --apply (it edits notes
across the real vault, so a bare run must not: before 10 Nov 2026 it did).
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import settings  # noqa: E402  (the vault: DENISSE_VAULT, config.json, or Obsidian's registry)

VAULT = Path(settings.vault())
RENAMES = {"memory_bank_vault": "vault_setup", "denisse_vault": "README"}
WRAPPED = re.compile(r"\[\[([^\]\r\n]*)[\r\n]+[ \t]*([^\]\r\n]*?)\]\]")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: only list them)")
    apply = ap.parse_args(argv).apply
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    changed = 0

    for path in sorted(VAULT.rglob("*.md")):
        if ".obsidian" in path.parts:
            continue
        original = path.read_text(encoding="utf-8")
        text = original

        for old, new in RENAMES.items():
            text = text.replace(f"[[{old}]]", f"[[{new}]]")
            text = text.replace(f"[[{old}|", f"[[{new}|")

        text = WRAPPED.sub(
            lambda m: "[[" + " ".join((m.group(1) + " " + m.group(2)).split()) + "]]",
            text,
        )

        if text != original:
            if apply:
                path.write_text(text, encoding="utf-8")
            changed += 1
            print(f"{'fixed ' if apply else 'would fix'} {path.relative_to(VAULT)}")

    print(f"\n{changed} file(s) changed" if apply else
          f"\n{changed} file(s) would change; nothing written. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
