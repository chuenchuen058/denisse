"""File orphan vault notes into their area index. Dry run unless --apply.

    .venv\\Scripts\\python.exe scripts\\vault_index_fix.py [--vault PATH] [--apply]

The vault rule (AI-RULES.md): every note has "one line in exactly one area
index, `- [[Note-Name]] — one clause`". scripts/vault_contract.py reports the
notes nothing links to as orphans; this proposes the missing index line for
each and, with --apply, inserts it.

* Orphans come from vault_contract itself -- its scan section is executed
  against --vault (the report write and the prints are left out), so "orphan"
  means exactly what the contract check means by it.
* The index is the nearest `<Folder>/<Folder>-Index.md` walking up from the
  note's folder: `Atlas/Projects/x.md` -> `Atlas/Projects/Projects-Index.md`
  if it exists, else `Atlas/Atlas-Index.md`. An orphan index note is filed in
  the index above its own folder.
* The line is `- [[name]] — <frontmatter description, ~80 chars>`, or `TODO`
  when there is no description (the vault writes a missing fact as TODO).
* --apply only INSERTS lines: under the `### <folder>/` section that matches
  the note's folder if the index has one, else after the index's last list
  line, else at the end. Existing lines are never rewritten.
* Never touched: Denisse/ (written by Denisse only), .obsidian/, Templates/,
  vault_contract's GENERATED notes, dated notes under Calendar/, root notes.
"""
import argparse
import ast
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACT = os.path.join(HERE, "vault_contract.py")

# Folders this tool must never file from or write into, on top of GENERATED.
NEVER = ("Denisse/", ".obsidian/", "Templates/", ".git/", ".trash/")
DATED = re.compile(r"\d{4}-\d{2}-\d{2}")
DESC_MAX = 80


def _contract_tree():
    with open(CONTRACT, encoding="utf-8") as f:
        return ast.parse(f.read(), CONTRACT)


def _constant(name):
    """A module-level literal from vault_contract (VAULT, GENERATED, ...),
    read without running the script."""
    for st in _contract_tree().body:
        if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in st.targets):
            return ast.literal_eval(st.value)
    raise LookupError(f"vault_contract.py has no {name}")


def default_vault():
    """The vault vault_contract itself would check (settings.vault())."""
    sys.path.insert(0, os.path.dirname(HERE))
    import settings
    return settings.vault()


GENERATED = tuple(_constant("GENERATED"))


def contract_scan(vault):
    """Run vault_contract's scan (everything before its report section) with
    VAULT pointed at `vault`. Returns its namespace: warns, linked, md_files,
    by_name, frontmatter(), rel()...

    The script is module-level code with a hardcoded vault, so this is the
    same AST trick atrium/test_import_chats.py uses to run it on a fixture."""
    tree = _contract_tree()
    body = []
    for st in tree.body:
        if isinstance(st, ast.FunctionDef) and st.name == "block":
            break  # report section starts here: no write, no prints
        # `sys.stdout = TextIOWrapper(...)` would replace the caller's stdout
        if isinstance(st, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr == "stdout" for t in st.targets):
            continue
        if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VAULT" for t in st.targets):
            st.value = ast.Constant(vault)
        body.append(st)
    else:
        raise RuntimeError("vault_contract.py changed shape: no `def block` report section found")
    tree.body = body
    ns = {"__name__": "vault_contract_scan", "__file__": CONTRACT}
    exec(compile(ast.fix_missing_locations(tree), CONTRACT, "exec"), ns)
    return ns


def orphans(ns):
    return sorted({r for r, msg in ns["warns"] if msg.startswith("orphan")})


def skip_reason(r):
    if "/" not in r:
        return "vault root note"
    if r.startswith(NEVER):
        return f"excluded folder {r.split('/')[0]}/"
    if r.startswith(GENERATED):
        return "generated note"
    if r.startswith("Calendar/") and DATED.search(os.path.basename(r)):
        return "dated Calendar note"
    return None


def find_index(vault, r):
    """Nearest `<D>/<basename(D)>-Index.md` above the note, as a vault path."""
    folder = os.path.dirname(r)
    if os.path.basename(r) == f"{os.path.basename(folder)}-Index.md":
        folder = os.path.dirname(folder)  # an index files into the one above
    while folder:
        cand = f"{folder}/{os.path.basename(folder)}-Index.md"
        if cand != r and not cand.startswith(NEVER) and os.path.isfile(os.path.join(vault, cand.replace("/", os.sep))):
            return cand
        folder = os.path.dirname(folder)
    return None


def clause(desc):
    desc = (desc or "").strip()
    if len(desc) >= 2 and desc[0] == desc[-1] and desc[0] in "\"'":
        desc = desc[1:-1]
    desc = " ".join(desc.split())
    if not desc:
        return "TODO"
    if len(desc) > DESC_MAX:
        cut = desc[:DESC_MAX].rsplit(" ", 1)[0] or desc[:DESC_MAX]
        desc = cut.rstrip(" ,;:—-") + "…"
    return desc


def link_name(ns, r):
    """Bare basename when it resolves to one file, else the vault path --
    a bare link to an ambiguous name is itself a contract warning."""
    base = os.path.basename(r)[:-3]
    hits = set(ns["by_name"].get(base, []))
    return base if len(hits) <= 1 else r[:-3]


def plan(vault):
    """[(note, index, line)], [(note, reason)]"""
    vault = os.path.abspath(vault)
    ns = contract_scan(vault)
    todo, skipped = [], []
    for r in orphans(ns):
        why = skip_reason(r)
        index = None if why else find_index(vault, r)
        if not why and not index:
            why = "no area index above it"
        if why:
            skipped.append((r, why))
            continue
        with open(os.path.join(vault, r.replace("/", os.sep)), encoding="utf-8-sig", errors="replace") as f:
            fm = ns["frontmatter"](f.read()) or {}
        todo.append((r, index, f"- [[{link_name(ns, r)}]] — {clause(fm.get('description'))}"))
    return todo, skipped


def insert_lines(text, folder, new):
    """Return `text` with `new` lines inserted; existing lines untouched."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    if lines and lines[-1] == "":
        lines.pop()
    # Skip a note the index already links, so a re-run never doubles a line.
    new = [ln for ln in new if not re.search(
        r"\[\[" + re.escape(ln.split("[[", 1)[1].split("]]", 1)[0]) + r"[\]|#]", text)]
    if not new:
        return text
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
    lo, hi = body_start, len(lines)
    section = re.compile(r"^#{1,6}\s+`?" + re.escape(folder.rstrip("/") + "/") + r"`?\s*$")
    for i in range(body_start, len(lines)):
        if section.match(lines[i].strip()):
            lo = i + 1
            hi = next((j for j in range(lo, len(lines)) if re.match(r"^#{1,6}\s", lines[j])), len(lines))
            break
    last_item = None
    for i in range(lo, hi):
        if re.match(r"^\s*[-*]\s", lines[i]):
            last_item = i
    if last_item is not None:
        at = last_item + 1
    elif lo != body_start:          # matched section with no list yet
        at = lo
    else:
        at = len(lines)
        while at > body_start and not lines[at - 1].strip():
            at -= 1
        if at > body_start and not lines[at - 1].lstrip().startswith("#"):
            new = [""] + new        # keep the list apart from a paragraph
    lines[at:at] = new
    return nl.join(lines) + nl


def apply(vault, todo):
    vault = os.path.abspath(vault)
    by_index = {}
    for r, index, line in todo:
        by_index.setdefault(index, []).append((r, line))
    written = 0
    for index, items in sorted(by_index.items()):
        if index.startswith(NEVER) or index.startswith(GENERATED):
            continue
        path = os.path.join(vault, index.replace("/", os.sep))
        with open(path, "rb") as f:
            raw = f.read()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        out = text
        # One insertion per folder so each lands in its own section.
        by_folder = {}
        for r, line in items:
            by_folder.setdefault(os.path.dirname(r), []).append(line)
        for folder, lines in sorted(by_folder.items()):
            out = insert_lines(out, folder, lines)
        if out != text:
            with open(path, "wb") as f:
                f.write((b"\xef\xbb\xbf" if bom else b"") + out.encode("utf-8"))
            written += 1
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault", default=None, help="default: settings.vault()")
    ap.add_argument("--apply", action="store_true", help="insert the lines (default: dry run)")
    a = ap.parse_args(argv)
    a.vault = a.vault or default_vault()
    if not os.path.isdir(a.vault):
        print(f"no such vault: {a.vault}", file=sys.stderr)
        return 1
    out = sys.stdout
    if hasattr(out, "buffer") and (out.encoding or "").lower() not in ("utf-8", "utf8"):
        out = io.TextIOWrapper(out.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    todo, skipped = plan(a.vault)
    if todo:
        w = max(len(r) for r, _, _ in todo)
        wi = max(len(i) for _, i, _ in todo)
        print(f"{'note':<{w}}  {'index':<{wi}}  line", file=out)
        for r, index, line in todo:
            print(f"{r:<{w}}  {index:<{wi}}  {line}", file=out)
    print(f"\n{len(todo)} orphan(s) to file, {len(skipped)} skipped", file=out)
    for r, why in skipped:
        print(f"  skip  {r}: {why}", file=out)
    if a.apply:
        n = apply(a.vault, todo)
        print(f"applied: {len(todo)} line(s) into {n} index note(s)", file=out)
    else:
        print("dry run -- nothing written; --apply to insert", file=out)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
