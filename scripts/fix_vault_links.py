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

Idempotent. Prints what it changed.
"""

import re
import sys
from pathlib import Path

VAULT = Path(r"C:\Users\Xichu\OneDrive - King's College London\Denisse")
RENAMES = {"memory_bank_vault": "vault_setup", "denisse_vault": "README"}
WRAPPED = re.compile(r"\[\[([^\]\r\n]*)[\r\n]+[ \t]*([^\]\r\n]*?)\]\]")


def main() -> int:
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
            path.write_text(text, encoding="utf-8")
            changed += 1
            print(f"fixed  {path.relative_to(VAULT)}")

    print(f"\n{changed} file(s) changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
