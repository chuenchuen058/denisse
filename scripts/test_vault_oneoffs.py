"""The vault one-offs (normalize_vault.py, fix_vault_links.py) write nothing unless told to.
Temp folders only: each module's VAULT is pointed at one."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_vault_links  # noqa: E402
import normalize_vault  # noqa: E402

NOTE = "\n".join(["---", "name: a", "originSessionId: abc123", "---",
                  "See [[memory_bank_vault]] and [[Hey", "Denisse Wake Word]].", ""])


class DryRunByDefault(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name)
        self.note = self.vault / "a.md"
        self.note.write_bytes(NOTE.encode("utf-8"))

    def run_script(self, module, *args):
        out = io.StringIO()
        out.reconfigure = lambda **_kw: None  # main() reconfigures the real stdout's encoding
        with mock.patch.object(module, "VAULT", self.vault), contextlib.redirect_stdout(out):
            code = module.main(list(args))
        return code, out.getvalue()

    def test_a_bare_run_lists_the_change_and_writes_nothing(self):
        for module in (normalize_vault, fix_vault_links):
            with self.subTest(module=module.__name__):
                code, out = self.run_script(module)
                self.assertEqual(code, 0)
                self.assertEqual(self.note.read_bytes(), NOTE.encode("utf-8"))
                self.assertIn("a.md", out)
                self.assertIn("1 file(s) would change; nothing written", out)

    def test_apply_writes(self):
        code, out = self.run_script(fix_vault_links, "--apply")
        self.assertEqual(code, 0)
        text = self.note.read_text(encoding="utf-8")
        self.assertIn("[[vault_setup]]", text)
        self.assertIn("[[Hey Denisse Wake Word]]", text)
        self.assertIn("1 file(s) changed", out)
        code, out = self.run_script(normalize_vault, "--apply")
        self.assertEqual(code, 0)
        self.assertNotIn("originSessionId", self.note.read_text(encoding="utf-8"))
        self.assertIn("1 file(s) normalised", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
