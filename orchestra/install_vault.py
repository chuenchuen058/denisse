"""Add the Atlas skeleton to the Obsidian vault without touching anything already there.

    .venv\\Scripts\\python.exe orchestra\\install_vault.py                 # dry run: prints the plan
    .venv\\Scripts\\python.exe orchestra\\install_vault.py --apply         # creates what's missing

The kit (notes with Chuen's details) lives in .review/vault_kit/, which is git-ignored
because this repo is public. Rules:
  * a file that already exists is never overwritten, merged or moved
  * folders are only created, never renamed
  * pre-Atlas top-level folders and notes are listed for a later, approved migration
Quit Obsidian is NOT needed: nothing under .obsidian/ is written.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def default_vault() -> Path | None:
    for key in ("ORCHESTRA_VAULT_DIR", "DENISSE_VAULT"):
        value = os.environ.get(key)
        if value:
            return Path(os.path.expanduser(value))
    config_path = REPO / "orchestra" / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        value = config.get("vault_dir")
        if value:
            return Path(os.path.expanduser(str(value)))
    return None


DEFAULT_VAULT = default_vault()
ATLAS_TOP = {"Atlas", "Efforts", "Calendar", "Dev", "Denisse", "Templates", "Archive", ".obsidian", ".git", ".trash"}
ATLAS_ROOT_FILES = {"hot.md", "index.md", "AI-RULES.md", "AGENTS.md", "CLAUDE.md", "GEMINI.md", "STATE NOW.md"}


def plan(kit: Path, vault: Path) -> tuple[list[Path], list[Path]]:
    create, keep = [], []
    for src in sorted(kit.rglob("*")):
        if src.is_file():
            dst = vault / src.relative_to(kit)
            (keep if dst.exists() else create).append(src.relative_to(kit))
    return create, keep


def pre_atlas(vault: Path) -> list[str]:
    out = []
    for p in sorted(vault.iterdir()):
        if p.name in ATLAS_TOP or p.name in ATLAS_ROOT_FILES:
            continue
        out.append(p.name + ("/" if p.is_dir() else ""))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    ap.add_argument("--kit", type=Path, default=REPO / ".review" / "vault_kit")
    ap.add_argument("--apply", action="store_true", help="create the missing files (default: dry run)")
    a = ap.parse_args()
    if a.vault is None:
        print("vault not configured: set ORCHESTRA_VAULT_DIR / DENISSE_VAULT or orchestra/config.json vault_dir")
        return 2
    if not a.vault.is_dir():
        print(f"vault not found: {a.vault}")
        return 2
    if not a.kit.is_dir():
        print(f"kit not found: {a.kit}")
        return 2
    create, keep = plan(a.kit, a.vault)
    print(f"vault: {a.vault}")
    print(f"\nwill create ({len(create)}):" if not a.apply else f"\ncreating ({len(create)}):")
    for r in create:
        print(f"  + {r.as_posix()}")
        if a.apply:
            dst = a.vault / r
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():  # re-check: never overwrite
                shutil.copyfile(a.kit / r, dst)
    print(f"\nalready there, left untouched ({len(keep)}):")
    for r in keep:
        print(f"  = {r.as_posix()}")
    rest = pre_atlas(a.vault)
    print(f"\npre-Atlas items, not moved (migrate later, see Dev/Vault-Migration.md) ({len(rest)}):")
    for r in rest:
        print(f"  ? {r}")
    if not a.apply:
        print("\ndry run: nothing written. Add --apply to create the files above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
