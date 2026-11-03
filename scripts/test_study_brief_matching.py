"""study_brief.match_module: aliases, prefixes and typos, all from study_brief.json.

python -m unittest discover -s scripts -p "test_study_brief_matching.py" -v

Uses a temp vault only; his real vault is never read.
"""
import datetime as dt
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import planner  # noqa: E402
import study_brief as sb  # noqa: E402

SS, EM = "Signals and Systems", "Electromagnetism"
CM, CAB = "Computational Methods", "Computational Applied Biomathematics"


class Matching(unittest.TestCase):
    def setUp(self):
        self._vault = sb.VAULT
        sb.VAULT = tempfile.mkdtemp(prefix="denisse-match-")  # empty: config modules only
        sb._discovered["vault"] = None

    def tearDown(self):
        sb.VAULT = self._vault
        sb._discovered["vault"] = None

    def check(self, cases):
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(sb.match_module(text), want)

    def test_aliases_and_codes(self):
        self.check([("prep me for signals", SS), ("S&S coursework", SS), ("brief me on SnS", SS),
                    ("prep me for 5CCYB020", SS), ("brief me on em", EM), ("help me revise cab", CAB),
                    ("comp methods please", CM), ("signals and systems", SS)])

    def test_prefixes(self):
        self.check([("prep me for sig and sys", SS), ("sig & sys brief", SS), ("revise sig n sys", SS),
                    ("comp meth brief", CM), ("prep me for electro", EM),
                    ("comp applied biomaths", CAB)])

    def test_typos(self):
        self.check([("prep me for signls", SS), ("brief on elctromagnetism", EM),
                    ("revise biomathmatics", CAB)])

    def test_no_module(self):
        self.check([("tell them about the system", None), ("prep me for them", None),
                    ("plan my study modules", None), ("prep me for the JPMorgan interview", None),
                    ("help me revise this paragraph", None), ("the computational side", None),
                    ("signals and electromagnetism", None), ("", None)])

    def test_module_found_in_the_vault(self):
        """A module with dated coursework is known without editing the JSON."""
        path = os.path.join(sb.VAULT, "Atlas", "Academic", "SEM 2 Modules",
                            "5CCYB099 Quantum Widgets", "Coursework.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write('---\ndescription: "5CCYB099 coursework, due 24 Nov 2026"\n---\n')
        self.assertTrue(planner.coursework_deadlines(sb.VAULT))
        self.check([("brief me on quantum widgets", "Quantum Widgets"),
                    ("prep me for quant widg", "Quantum Widgets"),
                    ("5ccyb099 please", "Quantum Widgets"), ("prep me for signals", SS)])
        reply = sb.run("prep me for nothing in particular", dt.datetime(2026, 10, 30, 19))
        self.assertTrue(reply.startswith(sb.WHICH_MODULE))
        self.assertIn("Quantum Widgets", reply)


if __name__ == "__main__":
    unittest.main()
