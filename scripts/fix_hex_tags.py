"""Stop colour hexes being read as tags.

A bare `#a855f7` in a note is a TAG to Obsidian, so the vault's tag list and
the colored-tags plugin had entries called 4c1d95, a855f7, c9a3ff, ff6b70,
ffcb52 and 7fb3ff. The fix is to write them as code, which is what they are.

Occurrences already inside backticks or a fenced block are left alone.
--write to apply.
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
V = r"C:\Users\Xichu\OneDrive - King's College London\Denisse"
WRITE = "--write" in sys.argv
HEX = re.compile(r"(?<![\w`#])#([0-9a-fA-F]{6})\b(?!`)")


def spans_to_skip(text):
    """Character ranges that are code, so already safe."""
    out = []
    for m in re.finditer(r"```.*?```|~~~.*?~~~|`[^`\n]*`", text, re.S):
        out.append((m.start(), m.end()))
    return out


total = files = 0
for dirpath, dirnames, filenames in os.walk(V):
    dirnames[:] = [d for d in dirnames if d not in (".obsidian", ".git", ".trash")]
    for fn in sorted(filenames):
        if not fn.endswith(".md"):
            continue
        path = os.path.join(dirpath, fn)
        with open(path, encoding="utf-8-sig") as f:
            text = original = f.read()
        skip = spans_to_skip(text)
        hits = [m for m in HEX.finditer(text)
                if not any(a <= m.start() < b for a, b in skip)]
        if not hits:
            continue
        # back to front, so earlier offsets stay valid
        for m in reversed(hits):
            text = text[:m.start()] + f"`#{m.group(1)}`" + text[m.end():]
        files += 1
        total += len(hits)
        rel = path.replace(V + os.sep, "").replace(os.sep, "/")
        print(f"  {'fixed' if WRITE else 'would fix'} {len(hits)}x  {rel}")
        for m in hits:
            print(f"      {text[max(0, m.start() - 45):m.start() + 20].splitlines()[-1].strip()}")
        if WRITE:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)

print(f"\n{total} bare hex colours in {files} notes"
      + ("" if WRITE else " -- re-run with --write"))
