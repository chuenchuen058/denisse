"""
One-off: normalise vault frontmatter to a tool-agnostic schema.

The notes had accumulated assistant-specific metadata -- `originSessionId`
from a Claude Code session, and a `metadata:` block whose `node_type` /
`type` keys meant something to one tool and nothing to any other. That is
exactly the kind of thing that makes a vault feel like it belongs to one
vendor.

Target schema, the same for every note:

    ---
    name: <slug>
    description: <one line>
    updated: <YYYY-MM-DD>
    tags: [...]          # only if the note already had them
    aliases: [...]       # only if the note already had them
    ---

Idempotent -- safe to re-run. Prints what it changed.
"""

import datetime
import re
import sys
from pathlib import Path

VAULT = Path(r"C:\Users\Xichu\OneDrive - King's College London\Denisse")
DROP_KEYS = {"originSessionId", "node_type", "type", "modified", "metadata"}
KEEP_ORDER = ["name", "description", "tags", "aliases", "updated"]


def parse_frontmatter(text: str):
    """Return (fields, body). Flattens one level of nesting, so a
    `metadata:` block's children are lifted up before being filtered."""
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, re.DOTALL)
    if not m:
        return None, text
    fields, body = {}, text[m.end():]
    for raw in m.group(1).splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        key, _, value = raw.partition(":")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        fields[key] = value  # nested keys land here too, then get filtered
    return fields, body


def build_frontmatter(fields: dict, fallback_name: str, updated: str) -> str:
    out = {k: v for k, v in fields.items() if k not in DROP_KEYS and v}
    out.setdefault("name", fallback_name)
    # Never overwrite a real date. `updated` is what the Project Log
    # dashboard sorts by, so stamping today over it destroys the ordering
    # and the historical record -- done once, on 2026-09-05, and reverted.
    # Fall back to the dropped `modified` value before inventing one.
    if not out.get("updated"):
        existing = fields.get("modified") or ""
        out["updated"] = existing.split("T")[0].strip('"\'') or updated
    lines = ["---"]
    for key in KEEP_ORDER:
        if key in out:
            value = out[key]
            if key == "description" and not (value.startswith(('"', "'"))):
                value = '"' + value.replace('"', "'") + '"'
            lines.append(f"{key}: {value}")
    for key, value in out.items():  # anything unexpected, kept at the end
        if key not in KEEP_ORDER:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not VAULT.is_dir():
        print(f"vault not found: {VAULT}")
        return 1

    today = datetime.date.today().isoformat()
    changed = 0

    for path in sorted(VAULT.rglob("*.md")):
        # Skip the app's own synced mirror and anything Obsidian owns.
        parts = set(path.relative_to(VAULT).parts)
        if parts & {".obsidian", "Brain", "News", "Evals"}:
            continue

        text = path.read_text(encoding="utf-8")
        fields, body = parse_frontmatter(text)
        if fields is None:
            print(f"[skip] {path.relative_to(VAULT)} (no frontmatter)")
            continue

        dropped = sorted(k for k in fields if k in DROP_KEYS and fields.get(k) is not None)
        new_text = build_frontmatter(fields, path.stem, today) + body.lstrip("\n")
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            changed += 1
            note = f" dropped {dropped}" if dropped else ""
            print(f"[fix ] {path.relative_to(VAULT)}{note}")
        else:
            print(f"[ok  ] {path.relative_to(VAULT)}")

    print(f"\n{changed} file(s) normalised")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
