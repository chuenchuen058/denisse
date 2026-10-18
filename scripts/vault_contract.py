"""Check every vault note against the contract in Memory/vault_setup.md.

READ ONLY apart from the one report it writes to 'Denisse/Vault Health.md'.

The contract, as the vault actually practises it (2026-10-15):
  * every hand-written note has frontmatter with name, description, updated
    ('tags' is only a warning -- Memory/ deliberately carries none)
  * no two notes share a 'name' (the slug style and the Title Case style
    both exist on purpose, so the style itself isn't checked)
  * 'updated' is an ISO date, not in the future
  * a link to an ATTACHMENT (pdf, m, png...) is path-qualified from the
    vault root -- a bare attachment name is how the week-3 mess started
  * a link to a NOTE may be a bare basename, but it has to resolve, and
    resolve to exactly one file
  * every note is reachable: something links to it
"""
import io
import os
import re
import sys
from datetime import date

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

VAULT = r"C:\Users\Xichu\OneDrive - King's College London\Denisse"
REPORT = os.path.join(VAULT, "Denisse", "Vault Health.md")
REQUIRED = ("name", "description", "updated")  # tags are only a warning: Memory/ deliberately has none
# Entry points: nothing needs to link to these for them to be found.
ROOTS = {"README.md", "MEMORY.md", "Vault Health.md", "SEM 1 Modules.md", "Log.md", "AGENTS.md"}
# Written by a machine, one per day, and never meant to be hand-curated.
# They get the link checks (a broken link there still breaks) but not the
# frontmatter contract or the orphan check -- holding a dated eval file to a
# hand-written note's standard just buries the findings that matter.
GENERATED = (
    "Denisse/Evals/",
    "Denisse/Brain/",
    "Denisse/Task Archive/",
    "Denisse/Tasks.md",
    "Templates/",
)
ATTACH = (".pdf", ".m", ".png", ".jpg", ".jpeg", ".svg", ".xlsx", ".docx", ".pptx", ".csv", ".zip")

errors, warns, notes = [], [], []


def rel(p):
    return p.replace(VAULT + os.sep, "").replace(os.sep, "/")


def walk():
    md, other = [], []
    for dirpath, dirnames, filenames in os.walk(VAULT):
        dirnames[:] = [d for d in dirnames if d not in (".obsidian", ".git", ".trash")]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            (md if fn.endswith(".md") else other).append(p)
    return md, other


def strip_code(text):
    """Blank out fenced blocks and inline code so link syntax QUOTED as an
    example isn't read as a real link. AGENTS.md documents the convention
    with `[[note_name]]` in backticks; Obsidian doesn't link that, and
    neither should this."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"~~~.*?~~~", "", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", "", text)


def frontmatter(text):
    """Top-level key: value pairs from a --- block. Not a YAML parser; the
    vault only ever uses flat keys, and a real parser is not installed."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    fm = {}
    for line in text[3:end].splitlines():
        if not line.strip() or line.startswith("#") or line.startswith(" "):
            continue
        k, _, v = line.partition(":")
        if _:
            fm[k.strip()] = v.strip()
    return fm


md_files, other_files = walk()
# The report is generated, and quotes link syntax inside backticks -- scanning
# it would turn every finding into a fresh fake finding next run.
all_md = list(md_files)  # the report is still a valid LINK TARGET
md_files = [p for p in md_files if os.path.normpath(p) != os.path.normpath(REPORT)]
# basename -> paths, for resolving bare links the way Obsidian does
by_name = {}
for p in all_md + other_files:
    fn = os.path.basename(p)
    by_name.setdefault(fn, []).append(p)
    if fn.endswith(".md"):
        by_name.setdefault(fn[:-3], []).append(p)

seen_names = {}
linked = set()          # vault-relative paths something links to
files_only = []         # week notes still awaiting a write-up
LINK = re.compile(r"\[\[([^\]\|\\]+)(?=[\\\]|])")
MDLINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\n]+)\)")

for path in sorted(md_files):
    r = rel(path)
    # utf-8-sig: Log/Log.md starts with a BOM, and a BOM in front of the
    # '---' made this script report a frontmatter block that was there.
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()

    generated = r.startswith(GENERATED)
    fm = frontmatter(text)
    if generated:
        pass  # link checks only, below
    elif fm is None:
        errors.append((r, "no frontmatter block at all"))
    else:
        for key in REQUIRED:
            if not fm.get(key):
                errors.append((r, f"frontmatter is missing `{key}`"))
        if not fm.get("tags"):
            warns.append((r, "no `tags` -- it won't show up in any tag view"))
        if fm.get("name"):
            if fm["name"] in seen_names:
                errors.append((r, f"`name: {fm['name']}` is already used by `{seen_names[fm['name']]}`"))
            else:
                seen_names[fm["name"]] = r
        u = fm.get("updated", "")
        if u:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", u):
                warns.append((r, f"`updated: {u}` is not an ISO date"))
            elif u > date.today().isoformat():
                warns.append((r, f"`updated: {u}` is in the future"))
        if fm.get("status") == "files-only":
            files_only.append((r, fm.get("description", "")))

    # UTF-8 text written out as cp1252 once, so an em dash became three
    # characters. Visible in Obsidian, and it spreads on every re-save.
    hit = next((s for s in ("â€”", "â€™", "â€œ", "Â·", "ðŸ") if s in text), None)
    if hit:
        warns.append((r, f"mojibake (`{hit}`) -- was written out as cp1252 at some point"))

    for m in LINK.finditer(strip_code(text)):
        target = m.group(1).strip().rstrip("\\").strip()
        if not target or target.startswith("#"):
            continue
        is_attach = os.path.splitext(target)[1].lower() in ATTACH
        if "/" in target:
            full = os.path.join(VAULT, target.replace("/", os.sep))
            # Obsidian's own path-qualified form for a note leaves the
            # extension off: [[Log/2026-10-15]].
            if not os.path.exists(full) and os.path.exists(full + ".md"):
                full += ".md"
            if os.path.exists(full):
                linked.add(rel(full))
            else:
                errors.append((r, f"broken link `[[{target}]]` -- no such file"))
        elif is_attach:
            hits = by_name.get(target, [])
            if hits:
                linked.add(rel(hits[0]))
                warns.append((r, f"attachment `[[{target}]]` is not path-qualified"))
            else:
                errors.append((r, f"broken attachment link `[[{target}]]`"))
        else:
            hits = by_name.get(target, [])
            if not hits:
                errors.append((r, f"broken link `[[{target}]]` -- nothing of that name"))
            elif len(set(hits)) > 1:
                warns.append((r, f"`[[{target}]]` is ambiguous: {', '.join(rel(h) for h in sorted(set(hits)))}"))
                for h in hits:
                    linked.add(rel(h))
            else:
                linked.add(rel(hits[0]))

    # Memory/MEMORY.md indexes its folder with ordinary Markdown links --
    # [Profile](chuen_profile.md) -- which Obsidian follows just as happily.
    # Counting only wikilinks reported chuen_profile.md as an orphan when the
    # index links it on line 30.
    for m in MDLINK.finditer(strip_code(text)):
        t = m.group(1).split("#")[0].strip()
        if not t or "://" in t or t.startswith("mailto:"):
            continue
        full = os.path.normpath(os.path.join(os.path.dirname(path), t.replace("/", os.sep)))
        if not os.path.exists(full) and os.path.exists(full + ".md"):
            full += ".md"
        if os.path.exists(full):
            linked.add(rel(full))
        else:
            errors.append((r, f"broken Markdown link `{t}`"))

for path in sorted(md_files):
    r = rel(path)
    if os.path.basename(path) in ROOTS or r.startswith(GENERATED):
        continue
    if r not in linked:
        warns.append((r, "orphan -- no other note links to it"))

mod_root = "SEM 1 Modules/"
for path in sorted(other_files):
    r = rel(path)
    # .keats-manifest.json is sync state, not material -- nothing should link it.
    if os.path.basename(r).startswith("."):
        continue
    if r.startswith(mod_root) and r not in linked:
        notes.append((r, "filed but not linked from any note"))


# ---------------------------------------------------------------- report ----
def block(title, items, empty):
    out = [f"## {title}", ""]
    if not items:
        out += [empty, ""]
        return out
    last = None
    for r, msg in items:
        if r != last:
            out.append(f"**`{r}`**")
            last = r
        out.append(f"- {msg}")
    out.append("")
    return out


today = date.today().isoformat()
n_md, n_other = len(md_files), len(other_files)
headline = (
    "Everything passes." if not (errors or warns)
    else f"{len(errors)} to fix, {len(warns)} worth a look."
)

lines = [
    "---",
    "name: vault-health",
    'description: "Automated contract check of every note — broken links, missing frontmatter, orphans. Regenerated by scripts/vault_contract.py; edits here are overwritten."',
    "tags: [denisse, vault, automation]",
    f"updated: {today}",
    "---",
    "",
    "# Vault health",
    "",
    f"Checked {n_md} notes and {n_other} attachments on **{today}**. {headline}",
    "",
    "> [!info] This file is generated",
    "> `scripts/vault_contract.py` rewrites it on every run, so don't edit it —",
    "> fix the notes it names instead. Errors break something in Obsidian;",
    "> warnings are conventions slipping.",
    "",
]
lines += block("Errors", errors, "Nothing broken.")
lines += block("Warnings", warns, "No conventions slipping.")
lines += block("Awaiting a write-up", files_only, "Every week note is written up.")
lines += block("Filed but unlinked", notes, "Every module file is linked from a note.")

os.makedirs(os.path.dirname(REPORT), exist_ok=True)
with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(lines))

print(f"{n_md} notes, {n_other} attachments")
print(f"errors   : {len(errors)}")
print(f"warnings : {len(warns)}")
print(f"files-only week notes : {len(files_only)}")
print(f"unlinked module files : {len(notes)}")
print(f"\nreport -> {rel(REPORT)}")
for label, items in (("ERROR", errors), ("WARN", warns)):
    for r, msg in items:
        print(f"  {label}  {r}: {msg}")
sys.exit(1 if errors else 0)
