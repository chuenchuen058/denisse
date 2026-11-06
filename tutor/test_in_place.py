"""tutor/: lecture files he already keeps in the vault's Week / Reference folders
are read in place -- no KEATS inbox needed (Chuen, 5 Nov 2026: "just get it
from Obsidian").

Fixture vault only (test_tutor.Fixture): a temp vault, temp state, fake model.

    .venv\\Scripts\\python.exe -m unittest discover -s tutor -p "test_*.py" -v
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import unittest
import unittest.mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tutor import conf, extract, ingest, progress  # noqa: E402
from tutor.test_tutor import EM, MODULES, Fixture, make_pdf  # noqa: E402


def tree(root):
    out = set()
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            out.add((os.path.relpath(p, root), os.path.getsize(p), os.path.getmtime(p)))
    return out


class InPlace(Fixture):
    def setUp(self):
        super().setUp()
        # No inbox anywhere: only his own files in the vault.
        shutil.rmtree(self.inbox)
        shutil.rmtree(self.shared)
        self.em = os.path.join(self.vault, MODULES, EM)
        shutil.rmtree(os.path.join(self.em, "KEATS inbox"))
        self.em_module = next(m for m in conf.modules(self.paths) if m.code == "5CCYB010")
        self.pdfs = {}
        for rel, lines in {
            ("Week 1", "Lecture - Notes.pdf"): ["Coulomb's law and the electric field of point charges."],
            ("Week 2", "Tutorial - Questions.pdf"): ["Gauss's law questions for a charged sphere and a line."],
            ("Week 2", "Exercises - Part 1.pdf"): ["Exercise: the potential of a dipole, step by step."],
            ("Week 2", "Tutorial - Solutions (official).pdf"): ["Solutions: flux through the Gaussian surface."],
            # The folder names the week, not the '7' in the file name.
            ("Week 3", "Lecture 7 - Electrostatics.pdf"): ["Electrostatic energy and capacitance of plates."],
            # Reference: placed by its name (or first page), like any file.
            ("Reference", "Lecture slides week 4.pdf"): ["Magnetic materials: magnetisation and the H field."],
            # Not lecture material by its name: not read.
            ("Reference", "Syllabus 2026-27.pdf"): ["Module syllabus with no week in it at all, just admin."],
        }.items():
            path = os.path.join(self.em, *rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            make_pdf(path, lines)
            self.pdfs[rel] = path
        # Not a configured extension: ignored. His own note in a Week folder: untouched.
        with open(os.path.join(self.em, "Week 2", "Tutorial - sketch.png"), "wb") as f:
            f.write(b"\x89PNG not really" * 10)
        self.mine = os.path.join(self.em, "Week 1", "My week 1 notes.md")
        self.note(os.path.relpath(self.mine, self.vault),
                  "---\nname: my-week-1\ndescription: mine\nupdated: 2026-11-01\ntags: [module]\n---\n"
                  "Lecture notes I wrote myself about Coulomb's law, long enough to count as a note.\n")

    def test_week_folders_are_read_in_place_once(self):
        before = tree(self.em)
        result, log, said = self.run_ingest()
        self.assertNotIn("no inbox folder found", said)
        self.assertEqual(result["failed"], [])
        names = sorted(os.path.basename(p) for p in result["notes"])
        self.assertEqual(names, ["Electromagnetism Week 1 tutor.md", "Electromagnetism Week 2 tutor.md",
                                 "Electromagnetism Week 3 tutor.md", "Electromagnetism Week 4 tutor.md"])
        # Tutor notes go into his Week folders, next to his files.
        for w in (1, 2, 3):
            self.assertTrue(os.path.isfile(os.path.join(self.em, f"Week {w}", f"Electromagnetism Week {w} tutor.md")))
        week2 = self.read(os.path.join(self.em, "Week 2", "Electromagnetism Week 2 tutor.md"))
        for name in ("Tutorial - Questions.pdf", "Exercises - Part 1.pdf", "Tutorial - Solutions (official).pdf"):
            self.assertIn(name, week2)
        self.assertNotIn("sketch.png", week2)
        week3 = self.read(os.path.join(self.em, "Week 3", "Electromagnetism Week 3 tutor.md"))
        self.assertIn("Lecture 7 - Electrostatics.pdf", week3)
        self.assertFalse(os.path.exists(os.path.join(self.em, "Week 7")))
        self.assertNotIn("Syllabus", "".join(log))
        # Nothing of his was moved, renamed, copied or changed; only her notes were added.
        after = tree(self.em)
        self.assertTrue(before <= after, before - after)
        added = {rel for rel, _s, _m in after - before}
        self.assertTrue(all(rel.endswith(" tutor.md") or rel == "Electromagnetism tutor.md" for rel in added), added)

        # Second run: no file read, no model call.
        with unittest.mock.patch.object(extract, "read", wraps=extract.read) as reads:
            result2, log2, said2 = self.run_ingest()
        self.assertEqual(reads.call_count, 0)
        self.assertEqual(log2, [])
        self.assertEqual(result2["notes"], [])
        self.assertEqual(len(result2["unchanged"]), 4)

        # A changed file is read again, and only its week regenerates.
        make_pdf(self.pdfs[("Week 1", "Lecture - Notes.pdf")], ["Coulomb's law, revised, with superposition."])
        result3, log3, _ = self.run_ingest()
        self.assertEqual([os.path.basename(p) for p in result3["notes"]], ["Electromagnetism Week 1 tutor.md"])
        self.assertEqual(len(log3), 1)

    def test_same_file_in_inbox_and_week_folder_is_read_once(self):
        inbox = os.path.join(self.em, "KEATS inbox")
        os.makedirs(inbox)
        shutil.copy(self.pdfs[("Week 1", "Lecture - Notes.pdf")], os.path.join(inbox, "Week 1 lecture.pdf"))
        result, _log, _said = self.run_ingest()
        week1 = self.read(os.path.join(self.em, "Week 1", "Electromagnetism Week 1 tutor.md"))
        listed = week1.split(conf.CONFIG["note"]["sources_heading"], 1)[1].strip().split("\n\n", 1)[0].splitlines()
        self.assertEqual(len(listed), 1, listed)
        self.assertIn("Week 1/Lecture - Notes.pdf", listed[0])  # his vault copy, not the inbox one
        self.assertIn(os.path.join(self.em, "Week 1", "Electromagnetism Week 1 tutor.md"), result["notes"])

    def test_progress_counts_the_weeks(self):
        state = ingest.load_state(self.paths)
        modules = conf.modules(self.paths)
        lec = progress.lectures(self.paths, self.em_module, state, modules)
        # Before ingest: the weeks are there, none read yet.
        self.assertEqual(lec["on_keats"], [1, 2, 3, 4])
        self.assertEqual(lec["read"], [])
        self.run_ingest()
        state = ingest.load_state(self.paths)
        lec = progress.lectures(self.paths, self.em_module, state, modules)
        self.assertEqual(lec["read"], [1, 2, 3, 4])
        self.assertEqual(lec["unread"], [])
        out = io.StringIO()
        progress.run(self.paths, [self.em_module], today=self.today, say=lambda s: out.write(s + "\n"))
        self.assertIn(conf.CONFIG["say"]["study"].split("{")[0], out.getvalue())
        line = next(s for s in out.getvalue().splitlines() if self.em_module.title in s)
        self.assertIn("4", line)
        self.assertNotIn("0 of 0", out.getvalue())


if __name__ == "__main__":
    unittest.main()
