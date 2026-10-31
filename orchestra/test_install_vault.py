"""Tests for orchestra/install_vault.py: it never overwrites or moves anything in the vault.

    .venv\\Scripts\\python.exe -m unittest orchestra.test_install_vault -v
Each test uses a throwaway vault and kit, so the real vault is never touched.
"""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import install_vault  # noqa: E402

KIT = {
    "hot.md": b"# hot\n",
    "AI-RULES.md": b"rules\n",
    "Atlas/Maps/Home.md": b"home map\n",
    "Efforts/On/Denisse/Plan.md": b"plan\n",
    "Templates/Daily.md": b"daily\n",
}


def snapshot(root: Path) -> dict[str, bytes | None]:
    """Every path under root -> its bytes (None for folders)."""
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


class InstallVaultTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.vault, self.kit = base / "vault", base / "kit"
        self.vault.mkdir()
        for rel, data in KIT.items():
            p = self.kit / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        # Belt and braces: if --vault were ever dropped, the default must not be the real vault.
        patcher = mock.patch.object(install_vault, "DEFAULT_VAULT", base / "no-such-vault")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, *extra: str) -> tuple[int, str]:
        argv = ["install_vault.py", "--vault", str(self.vault), "--kit", str(self.kit), *extra]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            code = install_vault.main()
        return code, out.getvalue()

    def test_dry_run_creates_nothing(self):
        (self.vault / "Notes").mkdir()
        (self.vault / "Notes" / "old.md").write_bytes(b"old\n")
        before = snapshot(self.vault)
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(snapshot(self.vault), before)
        self.assertIn("dry run: nothing written", out)
        for rel in KIT:
            self.assertIn(f"+ {rel}", out)

    def test_dry_run_on_empty_vault_creates_nothing(self):
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_apply_creates_every_missing_file_with_nested_folders(self):
        code, out = self.run_main("--apply")
        self.assertEqual(code, 0)
        for rel, data in KIT.items():
            self.assertEqual((self.vault / rel).read_bytes(), data, rel)
        self.assertNotIn("dry run", out)

    def test_apply_keeps_existing_file_bytes(self):
        existing = {
            "hot.md": b"Chuen's own hot note\r\n\x00binary-ish\xff",
            "Atlas/Maps/Home.md": b"hand-edited map\n",
        }
        for rel, data in existing.items():
            p = self.vault / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        code, out = self.run_main("--apply")
        self.assertEqual(code, 0)
        for rel, data in existing.items():
            self.assertEqual((self.vault / rel).read_bytes(), data, rel)
            self.assertIn(f"= {rel}", out)
        for rel in set(KIT) - set(existing):
            self.assertEqual((self.vault / rel).read_bytes(), KIT[rel], rel)

    def test_apply_twice_changes_nothing_the_second_time(self):
        self.run_main("--apply")
        (self.vault / "hot.md").write_bytes(b"edited after install\n")
        before = snapshot(self.vault)
        code, _ = self.run_main("--apply")
        self.assertEqual(code, 0)
        self.assertEqual(snapshot(self.vault), before)

    def test_pre_atlas_items_reported_and_left_in_place(self):
        pre = {
            "Projects/a.md": b"project a\n",
            "Memory/MEMORY.md": b"index\n",
            "Loose note.md": b"loose\n",
        }
        for rel, data in pre.items():
            p = self.vault / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        for extra in ((), ("--apply",)):
            with self.subTest(apply=bool(extra)):
                code, out = self.run_main(*extra)
                self.assertEqual(code, 0)
                for name in ("? Projects/", "? Memory/", "? Loose note.md"):
                    self.assertIn(name, out)
                # Atlas names are not reported as pre-Atlas.
                self.assertNotIn("? hot.md", out)
                self.assertNotIn("? Atlas/", out)
                for rel, data in pre.items():
                    self.assertEqual((self.vault / rel).read_bytes(), data, rel)

    def test_missing_vault_writes_nothing(self):
        self.vault.rmdir()
        code, out = self.run_main("--apply")
        self.assertEqual(code, 2)
        self.assertIn("vault not found", out)
        self.assertFalse(self.vault.exists())


if __name__ == "__main__":
    unittest.main()
