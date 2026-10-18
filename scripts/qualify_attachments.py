"""Turn bare attachment wikilinks into path-qualified ones.

    [[Math Review (pt 1).pdf|Math review]]
 -> [[SEM 1 Modules/.../Math Review (pt 1).pdf|Math review]]

Only touches links whose basename resolves to exactly ONE file in the vault;
anything ambiguous is listed and left alone. Pass --write to apply; the
default is a dry run. Aliases after the | are untouched.
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

VAULT = r"C:\Users\Xichu\OneDrive - King's College London\Denisse"
EXT = "pdf|m|png|jpg|jpeg|svg|xlsx|docx|pptx|csv|zip"
LINK = re.compile(r"\[\[((?:[^\]\|\\/]+)\.(?:" + EXT + r"))(?=[\\\]|])", re.I)
WRITE = "--write" in sys.argv

md, by_name = [], {}
for dirpath, dirnames, filenames in os.walk(VAULT):
    dirnames[:] = [d for d in dirnames if d not in (".obsidian", ".git", ".trash")]
    for fn in filenames:
        p = os.path.join(dirpath, fn)
        # The health report quotes link syntax in its findings -- rewriting
        # those would be rewriting the complaint instead of the cause.
        if fn.endswith(".md") and fn != "Vault Health.md":
            md.append(p)
        by_name.setdefault(fn, []).append(p)


def rel(p):
    return p.replace(VAULT + os.sep, "").replace(os.sep, "/")


changed = fixed = skipped = 0
for path in sorted(md):
    with open(path, encoding="utf-8") as f:
        text = original = f.read()
    hits = list(LINK.finditer(text))
    if not hits:
        continue
    local = []
    for m in hits:
        name = m.group(1)
        targets = sorted(set(by_name.get(name, [])))
        if len(targets) != 1:
            print(f"  SKIP  {rel(path)}: [[{name}]] -> {len(targets)} matches")
            skipped += 1
            continue
        local.append((name, rel(targets[0])))
    # replace longest names first so one name isn't a prefix of another
    for name, full in sorted(set(local), key=lambda t: -len(t[0])):
        text = text.replace(f"[[{name}", f"[[{full}")
        fixed += 1
    if text != original:
        changed += 1
        print(f"  {'fixed' if WRITE else 'would fix'}  {rel(path)}")
        if WRITE:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)

print(f"\n{'wrote' if WRITE else 'dry run:'} {changed} notes, {fixed} links, {skipped} skipped")
if not WRITE:
    print("re-run with --write to apply")
