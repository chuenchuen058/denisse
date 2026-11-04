"""vault_search.json, planner.json and delegate.json drive behaviour (Rule 0).

Each test edits one value in a module's CONFIG and checks the behaviour
follows it, then restores the shipped data. Default-data behaviour is
guarded by test_planner.py, test_delegate.py and test_adaptive_brain.py.
Everything runs against a temp vault and fake engines: nothing is spent.
"""
import copy
import datetime as dt
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="denisse-softcode-")
os.environ.setdefault("DENISSE_VAULT", os.path.join(TMP, "vault"))

import delegate  # noqa: E402
import planner  # noqa: E402
import tasks  # noqa: E402
import vault_search  # noqa: E402

VAULT = os.path.join(TMP, "fake-vault")


def note(rel, text):
    p = os.path.join(VAULT, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


note("Notes/Optics.md", '---\ndescription: "optics notes"\n---\n'
     "Optics is taught on Mondays and the lecturer is very good at it.\n"
     "A second optics line that is long enough to be quoted back.\n"
     "A third optics line that is also long enough to be quoted.\n")
note("Notes/Waves.md", "---\ndescription: \"waves\"\n---\n"
     + "Optics optics optics optics optics optics optics optics optics appears in waves too.\n")
note("Archive/Old.md", "Optics in an archived note, long enough to be a quote line.\n")
note("Atlas/Academic/SEM 1 Modules/5CCYB010 Electromagnetism/Coursework.md",
     '---\ndescription: "5CCYB010 coursework, 30%, due 24 Nov 2026 - brief not yet read"\n---\n')
note("Elsewhere/Modules/Optics/Coursework.md", '---\ndescription: "due 20 Nov 2026"\n---\n')

tasks.TASKS_FILE = os.path.join(TMP, "tasks.json")
tasks._mirror_to_vault = lambda: None
planner.PENDING_FILE = os.path.join(TMP, "pending_plan.json")


class _Config(unittest.TestCase):
    module = None

    def setUp(self):
        self._saved = copy.deepcopy(self.module.CONFIG)

    def tearDown(self):
        self.module.CONFIG.clear()
        self.module.CONFIG.update(self._saved)


class VaultSearchData(_Config):
    module = vault_search

    def setUp(self):
        super().setUp()
        self._vault = vault_search.VAULT
        vault_search.VAULT = VAULT

    def tearDown(self):
        vault_search.VAULT = self._vault
        super().tearDown()

    def test_stop_words_come_from_data(self):
        self.assertIn("lecturer", vault_search._words("optics lecturer"))
        vault_search.CONFIG["stop_words"].append("lecturer")
        self.assertNotIn("lecturer", vault_search._words("optics lecturer"))

    def test_stemming_comes_from_data(self):
        self.assertEqual(vault_search._stem("studies"), "study")
        vault_search.CONFIG["stem_replacements"] = {"ies": "ie"}
        self.assertEqual(vault_search._stem("studies"), "studie")

    def test_skip_prefixes_come_from_data(self):
        self.assertIn("Archive/Old.md", vault_search.search_vault("archived"))
        vault_search.CONFIG["skip_prefixes"].append("Archive")
        self.assertNotIn("Archive/Old.md", vault_search.search_vault("archived"))

    def test_scoring_weights_come_from_data(self):
        def first(out):
            return out.split("\n\n")[1].splitlines()[0]
        # Body hits are capped, so the note naming optics in its path wins.
        self.assertEqual(first(vault_search.search_vault("optics")), "Notes/Optics.md")
        vault_search.CONFIG["scoring"]["body_cap"] = 100
        vault_search.CONFIG["scoring"]["meta_weight"] = 0
        self.assertEqual(first(vault_search.search_vault("optics")), "Notes/Waves.md")

    def test_lines_per_note_come_from_data(self):
        def quoted(out):
            return out.split("\n\n")[1].count("\n  - ")
        self.assertEqual(quoted(vault_search.search_vault("optics")), 3)
        vault_search.CONFIG["lines"]["per_note"] = 1
        self.assertEqual(quoted(vault_search.search_vault("optics")), 1)

    def test_overview_phrases_come_from_data(self):
        self.assertFalse(vault_search.is_overview_query("whats new"))
        vault_search.CONFIG["overview_exact"].append("whats new")
        self.assertTrue(vault_search.is_overview_query("whats new"))

    def test_wording_comes_from_data(self):
        vault_search.CONFIG["messages"]["no_hits"] = "none: {words}"
        self.assertEqual(vault_search.search_vault("zzqqx"), "none: zzqqx")
        vault_search.CONFIG["state_note"] = "Nope/STATE.md"
        self.assertIn("Nope/STATE.md", vault_search.search_vault("my vault"))


class PlannerData(_Config):
    module = planner

    def deadlines(self):
        return planner.coursework_deadlines(VAULT)

    def test_coursework_glob_comes_from_data(self):
        self.assertEqual([d["module"] for d in self.deadlines()], ["Electromagnetism"])
        planner.CONFIG["coursework_glob"] = "Elsewhere/**/Coursework.md"
        self.assertEqual([d["module"] for d in self.deadlines()], ["Optics"])

    def test_note_markers_come_from_data(self):
        self.assertTrue(self.deadlines()[0]["brief_unread"])
        planner.CONFIG["brief_unread_marker"] = "never written"
        self.assertFalse(self.deadlines()[0]["brief_unread"])

    def test_when_words_come_from_data(self):
        today = dt.date(2026, 11, 4)
        self.assertEqual(planner._when(today, today), "today")
        planner.CONFIG["when"]["future"] = "{days}d out"
        self.assertEqual(planner._when(today + dt.timedelta(days=3), today), "3d out")

    def test_plan_offsets_come_from_data(self):
        orig = planner.coursework_deadlines
        planner.coursework_deadlines = lambda vault=VAULT: orig(vault)
        try:
            now = dt.datetime(2026, 10, 29, 11, 0)
            _, steps = planner.study_plan(now)
            draft = [s for s in steps if s["text"].startswith("Full draft")]
            self.assertEqual(draft[0]["due"], "17 Nov 2026 18:00")
            planner.CONFIG["study_plan"]["draft_before_due_days"] = 3
            planner.CONFIG["study_plan"]["step_due_time"] = "09:30"
            _, steps = planner.study_plan(now)
            draft = [s for s in steps if s["text"].startswith("Full draft")]
            self.assertEqual(draft[0]["due"], "21 Nov 2026 09:30")
            planner.CONFIG["study_plan"]["horizon_days"] = 5
            text, steps = planner.study_plan(now)
            self.assertEqual(steps, [])
            self.assertEqual(text, planner.CONFIG["study_plan"]["none"])
        finally:
            planner.coursework_deadlines = orig

    def test_pending_window_comes_from_data(self):
        now = dt.datetime(2026, 11, 4, 9, 0)
        planner.offer([{"text": "x", "due": "1 Dec 2026 18:00"}], now)
        later = now + dt.timedelta(minutes=45)
        self.assertEqual(planner.pending(later), [])
        planner.CONFIG["pending_minutes"] = 60
        self.assertEqual(len(planner.pending(later)), 1)
        planner.clear_pending()

    def test_offer_wording_comes_from_data(self):
        planner.CONFIG["offer"] = "Add {n}?"
        self.assertEqual(planner.offer([{"text": "x", "due": "1 Dec 2026 18:00"}]), "Add 1?")
        planner.clear_pending()


class DelegateData(_Config):
    module = delegate

    def setUp(self):
        super().setUp()
        self._which = delegate.shutil.which
        delegate.shutil.which = lambda name: {"claude": "C:/fake/claude.exe",
                                              "codex": "C:/fake/codex.cmd",
                                              "other": "C:/fake/other.exe"}.get(name)
        self._drafts = delegate.DRAFTS
        delegate.DRAFTS = os.path.join(TMP, "Drafts")

    def tearDown(self):
        delegate.shutil.which = self._which
        delegate.DRAFTS = self._drafts
        delegate.DRY_RUN = False
        super().tearDown()

    def test_engine_arguments_come_from_data(self):
        cmd, stdin = delegate._command("claude", "P", "out")
        self.assertIn("--output-format", cmd)
        self.assertIsNone(stdin)
        delegate.CONFIG["engines"]["claude"]["args"] = ["--ask", "{prompt}"]
        delegate.CONFIG["engines"]["claude"]["stdin"] = True
        cmd, stdin = delegate._command("claude", "P", "out")
        self.assertEqual(cmd[1:3], ["--ask", "P"])
        self.assertNotIn("--output-format", cmd)
        self.assertTrue(stdin.endswith(b"P"))

    def test_engine_list_comes_from_data(self):
        self.assertEqual(delegate._command("other", "P"), (None, None))
        delegate.CONFIG["engines"]["other"] = {"label": "Other", "exe": "other",
                                               "args": ["{prompt}"], "add_dir_flag": ""}
        self.assertEqual(delegate._command("other", "P")[0], ["C:/fake/other.exe", "P"])
        seen = []
        real_run = delegate.subprocess.run
        delegate.subprocess.run = lambda cmd, **kw: (
            seen.append(cmd[0]),
            delegate.subprocess.CompletedProcess(cmd, 0, b"x" * 100, b""))[1]
        delegate.CONFIG["engines"] = {"other": delegate.CONFIG["engines"]["other"]}
        log = delegate.LOG_FILE
        delegate.LOG_FILE = os.path.join(TMP, "delegate.log")
        try:
            ok, _, engine, _ = delegate.run("job", [])
        finally:
            delegate.subprocess.run = real_run
            delegate.LOG_FILE = log
        self.assertTrue(ok)
        self.assertEqual((engine, seen), ("other", ["C:/fake/other.exe"]))

    def test_non_answer_pattern_comes_from_data(self):
        text = "Here is the thing you wanted, written out at full length for you."
        self.assertFalse(delegate._is_non_answer(text))
        delegate.CONFIG["non_answer_pattern"] = "(?i)thing you wanted"
        self.assertTrue(delegate._is_non_answer(text))
        delegate.CONFIG["non_answer_min_chars"] = 1000
        self.assertTrue(delegate._is_non_answer("x" * 500))

    def test_draft_shape_comes_from_data(self):
        now = dt.datetime(2026, 11, 4, 10, 5)
        path = delegate.save("please write my cv", "body", "codex", now)
        self.assertEqual(os.path.basename(path), "2026-11-04 1005 write cv.md")
        with open(path, encoding="utf-8") as f:
            self.assertIn("Draft by Codex", f.read())
        delegate.CONFIG["slug_stop_words"] = []
        delegate.CONFIG["draft_body"] = "{who}|{text}"
        delegate.CONFIG["engines"]["codex"]["label"] = "Robot"
        path = delegate.save("please write my cv", "body", "codex", now)
        self.assertEqual(os.path.basename(path), "2026-11-04 1005 please write my cv.md")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "Robot|body")

    def test_what_she_says_comes_from_data(self):
        delegate.DRY_RUN = True
        delegate.CONFIG["say"]["dry_run"] = "off to {drafts}"
        delegate.CONFIG["drafts_folder"] = "Out/Box"
        self.assertEqual(delegate.start("job"), "off to Out/Box")
        delegate.shutil.which = lambda name: None
        delegate.CONFIG["say"]["not_installed"] = "nobody home"
        self.assertEqual(delegate.start("job"), "nobody home")


if __name__ == "__main__":
    unittest.main()
