"""
Audit the vault's link graph -- what Obsidian's graph view is actually drawing.

Scattered dots in that view are one of two things:
  orphans  -- a note with no links in and none out
  phantoms -- a [[link]] to a note that doesn't exist, drawn as a hollow node

Both are worth knowing about: phantoms are usually a rename that didn't get
followed through, orphans are usually machine-generated files that nothing
was ever meant to link to.
"""

import re
import sys
from collections import defaultdict
from pathlib import Path

VAULT = Path(r"C:\Users\Xichu\OneDrive - King's College London\Denisse")
WIKILINK = re.compile(r"\[\[([^\]|#\r\n]+)")  # a newline breaks a wikilink
MDLINK = re.compile(r"\[[^\]]*\]\(([^)]+?\.md)\)")
FENCE = re.compile(r"```.*?```", re.DOTALL)
CODESPAN = re.compile(r"`[^`\n]*`")


def strip_code(text: str) -> str:
    """Obsidian doesn't link inside code fences or code spans, so neither
    should this audit -- otherwise a documented example like the literal
    text for a wikilink is reported as a broken link."""
    return CODESPAN.sub(" ", FENCE.sub(" ", text))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    notes = [p for p in VAULT.rglob("*.md") if ".obsidian" not in p.parts]
    by_stem = defaultdict(list)
    for p in notes:
        by_stem[p.stem.lower()].append(p)

    outbound = defaultdict(set)
    inbound = defaultdict(set)
    phantoms = defaultdict(list)

    for p in notes:
        text = strip_code(p.read_text(encoding="utf-8", errors="replace"))
        targets = set()

        for raw in WIKILINK.findall(text):
            stem = Path(raw.strip()).stem.lower()
            if stem in by_stem:
                targets.add(by_stem[stem][0])
            else:
                phantoms[raw.strip()].append(p)

        for raw in MDLINK.findall(text):
            if raw.startswith(("http://", "https://")):
                continue
            dest = (p.parent / raw).resolve()
            if dest.exists():
                targets.add(dest)
            else:
                phantoms[raw].append(p)

        targets.discard(p)
        outbound[p] = targets
        for t in targets:
            inbound[t].add(p)

    def rel(p):
        try:
            return str(p.relative_to(VAULT))
        except ValueError:
            return str(p)

    orphans = [p for p in notes if not outbound[p] and not inbound[p]]
    sinks = [p for p in notes if not inbound[p] and outbound[p]]

    print(f"{len(notes)} notes, {sum(len(v) for v in outbound.values())} links\n")

    print(f"== ORPHANS ({len(orphans)}) -- the scattered dots, no links either way")
    groups = defaultdict(list)
    for p in orphans:
        groups[rel(p.parent)].append(p.name)
    for folder, names in sorted(groups.items()):
        print(f"  {folder or '.'}/  ({len(names)})")
        for n in sorted(names)[:4]:
            print(f"      {n}")
        if len(names) > 4:
            print(f"      ... and {len(names) - 4} more")

    print(f"\n== PHANTOM LINKS ({len(phantoms)}) -- hollow nodes, target doesn't exist")
    for target, sources in sorted(phantoms.items()):
        print(f"  [[{target}]]  <- {', '.join(rel(s) for s in sources[:3])}")

    print(f"\n== LINKED BUT NOTHING POINTS AT THEM ({len(sinks)})")
    for p in sorted(sinks, key=rel):
        print(f"  {rel(p)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
