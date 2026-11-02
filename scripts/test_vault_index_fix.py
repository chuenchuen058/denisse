"""scripts/vault_index_fix.py against a throwaway vault, never the real one."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vault_index_fix as fix  # noqa: E402


def note(name, desc="", body=""):
    fm = f"---\nname: {name}\n" + (f"description: {desc}\n" if desc else "") + "updated: 2026-11-01\n---\n"
    return fm + f"\n# {name}\n\n{body}\n"


INDEX = lambda area, items="": (  # noqa: E731
    f"---\nname: {area}-Index\ndescription: Index of {area}.\nupdated: 2026-10-31\n---\n\n# {area} index\n\n{items}")


class VaultIndexFixTest(unittest.TestCase):
    def setUp(self):
        self.vault = tempfile.mkdtemp(prefix="vault_index_fix_")
        self.addCleanup(shutil.rmtree, self.vault, True)
        self.write("index.md", note("index", "root", "[[Atlas-Index]] [[Efforts-Index]] [[Calendar-Index]]"))
        self.write("Atlas/Atlas-Index.md", INDEX("Atlas", "### Areas\n- [[Projects-Index]] — projects\n"))
        self.write("Atlas/Projects/Projects-Index.md", INDEX("Projects", "- [[Known]] — already filed\n"))
        self.write("Atlas/Projects/Known.md", note("Known", "filed already"))
        self.write("Atlas/Projects/Robot-Tail.md", note("Robot-Tail", '"A robotic tail: servo build, '
                                                       'Arduino control and a very long tail of words beyond eighty"'))
        self.write("Atlas/Projects/Deep/Sub-Note.md", note("Sub-Note", "Deep under Projects"))
        self.write("Atlas/Loose.md", note("Loose", "Sits in Atlas directly"))
        self.write("Efforts/Efforts-Index.md", INDEX("Efforts", "- TODO: something\n\nTrailing paragraph.\n"))
        self.write("Efforts/Internship.md", note("Internship"))  # no description
        self.write("Calendar/Calendar-Index.md", INDEX("Calendar"))
        # excluded
        self.write("Denisse/Denisse-Index.md", INDEX("Denisse"))
        self.write("Denisse/Mine.md", note("Mine", "Denisse writes this"))
        self.write("Templates/Templates-Index.md", INDEX("Templates"))
        self.write("Templates/T.md", note("T", "a template"))
        self.write("Calendar/Log/2026-10-07.md", note("log-2026-10-07", "dated"))
        self.write("Calendar/Handoff-2026-11-02.md", note("handoff", "generated"))
        self.write(".obsidian/hidden.md", note("hidden", "app state"))

    def write(self, r, text):
        p = os.path.join(self.vault, r.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def read(self, r):
        with open(os.path.join(self.vault, r.replace("/", os.sep)), encoding="utf-8") as f:
            return f.read()

    def snapshot(self):
        out = {}
        for d, _, fs in os.walk(self.vault):
            for fn in fs:
                p = os.path.join(d, fn)
                with open(p, "rb") as f:
                    out[os.path.relpath(p, self.vault)] = f.read()
        return out

    def run_main(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fix.main(["--vault", self.vault, *args])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_orphan_filed_in_nearest_index(self):
        todo, _ = fix.plan(self.vault)
        where = {r: i for r, i, _ in todo}
        self.assertEqual(where["Atlas/Projects/Robot-Tail.md"], "Atlas/Projects/Projects-Index.md")
        self.assertEqual(where["Atlas/Projects/Deep/Sub-Note.md"], "Atlas/Projects/Projects-Index.md")
        self.assertEqual(where["Atlas/Loose.md"], "Atlas/Atlas-Index.md")
        self.assertEqual(where["Efforts/Internship.md"], "Efforts/Efforts-Index.md")
        self.assertNotIn("Atlas/Projects/Known.md", where)  # already linked: not an orphan

        self.run_main("--apply")
        proj = self.read("Atlas/Projects/Projects-Index.md")
        line = next(ln for ln in proj.splitlines() if "[[Robot-Tail]]" in ln)
        self.assertTrue(line.startswith("- [[Robot-Tail]] — A robotic tail: servo build"))
        self.assertTrue(line.endswith("…"))
        self.assertLessEqual(len(line.split(" — ", 1)[1]), fix.DESC_MAX + 1)
        self.assertNotIn('"', line)
        self.assertIn("- [[Sub-Note]] — Deep under Projects", proj)
        self.assertIn("- [[Loose]] — Sits in Atlas directly", self.read("Atlas/Atlas-Index.md"))
        self.assertIn("- [[Internship]] — TODO", self.read("Efforts/Efforts-Index.md"))
        # filed notes are no longer orphans by vault_contract's own measure
        self.assertFalse(set(fix.orphans(fix.contract_scan(os.path.abspath(self.vault))))
                         & {"Atlas/Projects/Robot-Tail.md", "Atlas/Loose.md", "Efforts/Internship.md"})

    def test_existing_lines_untouched(self):
        before = {r: self.read(r).splitlines() for r in
                  ("Atlas/Projects/Projects-Index.md", "Atlas/Atlas-Index.md", "Efforts/Efforts-Index.md")}
        self.run_main("--apply")
        for r, old in before.items():
            new = self.read(r).splitlines()
            added = [ln for ln in new if ln not in old]
            self.assertTrue(added, r)
            self.assertTrue(all(ln.startswith("- [[") or ln == "" for ln in added), added)
            self.assertEqual([ln for ln in new if ln in old], old, f"{r}: an existing line moved or changed")
        # appended under the list, not after the trailing paragraph
        eff = self.read("Efforts/Efforts-Index.md").splitlines()
        self.assertEqual(eff[eff.index("- TODO: something") + 1], "- [[Internship]] — TODO")
        self.assertEqual(eff[-1], "Trailing paragraph.")

    def test_excluded_folders_skipped(self):
        before = self.snapshot()
        todo, skipped = fix.plan(self.vault)
        filed = {r for r, _, _ in todo}
        reasons = dict(skipped)
        for r in ("Denisse/Mine.md", "Templates/T.md", "Calendar/Log/2026-10-07.md"):
            self.assertNotIn(r, filed)
        self.assertIn("Denisse/Mine.md", reasons)
        self.assertIn("Calendar/Log/2026-10-07.md", reasons)
        self.assertFalse(any(r.startswith(".obsidian") for r in filed))
        self.run_main("--apply")
        after = self.snapshot()
        for r in before:
            p = r.replace(os.sep, "/")
            if p.startswith(("Denisse/", "Templates/", ".obsidian/", "Calendar/")):
                self.assertEqual(before[r], after[r], p)

    def test_idempotent(self):
        self.run_main("--apply")
        once = self.snapshot()
        out = self.run_main("--apply")
        self.assertEqual(once, self.snapshot())
        self.assertIn("0 orphan(s) to file", out)
        # and the insert itself refuses a note the index already links
        text = self.read("Atlas/Projects/Projects-Index.md")
        self.assertEqual(fix.insert_lines(text, "Atlas/Projects", ["- [[Known]] — again"]), text)

    def test_dry_run_writes_nothing(self):
        before = self.snapshot()
        out = self.run_main()
        self.assertEqual(before, self.snapshot())
        self.assertIn("Atlas/Projects/Robot-Tail.md", out)
        self.assertIn("Atlas/Projects/Projects-Index.md", out)
        self.assertIn("dry run", out)

    def test_folder_section_and_crlf(self):
        self.write("Calendar/Calendar-Index.md", INDEX("Calendar", "### Calendar/Orchestra/\n- one\n\n### Calendar/Other/\n- two\n"
                                                       ).replace("\n", "\r\n"))
        self.write("Calendar/Orchestra/Plan.md", note("Plan", "orchestra plan"))
        self.run_main("--apply")
        with open(os.path.join(self.vault, "Calendar", "Calendar-Index.md"), "rb") as f:
            raw = f.read().decode("utf-8")  # binary: text mode would hide CRLF
        lines = raw.replace("\r\n", "\n").split("\n")
        self.assertEqual(lines[lines.index("- one") + 1], "- [[Plan]] — orchestra plan")
        self.assertNotIn("\n", raw.replace("\r\n", ""))  # line endings kept

    def test_ambiguous_name_is_path_qualified(self):
        self.write("Efforts/Robot-Tail.md", note("Robot-Tail-2", "same basename"))
        todo, _ = fix.plan(self.vault)
        lines = {r: ln for r, _, ln in todo}
        self.assertIn("[[Efforts/Robot-Tail]]", lines["Efforts/Robot-Tail.md"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
