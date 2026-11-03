"""scripts/next_lecture.py against a fixture vault in the Atlas layout.

Never touches the real vault: every call is given a temp-dir vault.
"""
import ast
import datetime as dt
import glob
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import next_lecture as nl  # noqa: E402

TODAY = dt.date(2026, 11, 3)
EM = os.path.join("Atlas", "Academic", "SEM 1 Modules", "5CCYB010 Electromagnetism")
CM = os.path.join("Atlas", "Academic", "SEM 1 Modules", "5CCYB070 Computational Methods (26~27 SY)")
SS = os.path.join("Atlas", "Academic", "SEM 1 Modules", "5CCYB020 Signals and Systems")


def week_note(module, n, status="", extra="", body=""):
    lines = ["---", f"module: {module}", f"week: {n}", f"topic: {module} topic {n}"]
    if status:
        lines.append(f"status: {status}")
    if extra:
        lines.append(extra)
    return "\n".join(lines + ["---", "", f"# Week {n}", "", body]) + "\n"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.vault = tempfile.mkdtemp(prefix="denisse-nextlec-")
        self.addCleanup(shutil.rmtree, self.vault, True)
        # Electromagnetism: 1-3 written up, 4 and 5 files-only -> Week 5.
        self.note(EM, "Electromagnetism notes.md",
                  "---\nname: hub\n---\n| 1 | [[Electromagnetism Week 1]] |\n"
                  "- [[Electromagnetism concepts]] — every key idea\n- [[SEM 1 Modules]]\n")
        self.note(EM, "Electromagnetism concepts.md", "\n".join([
            "---", "name: electromagnetism-concepts", "tags: [module, concept]", "---",
            "## Part 1", "",
            "### Curl", "Curl of a magnetic field.", "- [[Electromagnetism Week 2]] — eq. 40", "",
            "### Gauss's law", "Flux through a surface.", "- [[Electromagnetism Week 3]] — eq. 39", "",
            "### Divergence", "Scalar out of a vector.", "- [[Electromagnetism Week 1]] — eq. 9", "",
            "### Faraday's law", "Changing flux induces an EMF.", "- [[Electromagnetism Week 5]]", "",
            "### Unrelated", "Nothing shared at all.", "- [[Electromagnetism Week 1]]", ""]))
        for n in (1, 2, 3):
            self.note(EM, f"Week {n}/Electromagnetism Week {n}.md", week_note("Electromagnetism", n))
        self.note(EM, "Week 4/Electromagnetism Week 4.md", week_note(
            "Electromagnetism", 4, "files-only",
            body="> [!todo] Lecture write-up still to do\n> files are here.\n\n"
                 "> [!todo]- Check the tutorial numbering\n> older edition.\n\n> [!note] Not a todo\n"))
        self.note(EM, "Week 5/Electromagnetism Week 5.md", week_note(
            "Electromagnetism", 5, "files-only", extra="topic2: x").replace(
            "topic: Electromagnetism topic 5", "topic: Electromagnetic induction and curl"))
        for fn in ("Lecture - Notes.pdf", "Lecture - Induction motors.pdf", "Tutorial - Questions.pdf",
                   "Tutorial - Solutions (worked).pdf", "Exercises - Sheet on flux.pdf",
                   "Notes - Lenz law.pdf", "random.png"):
            self.note(EM, f"Week 5/{fn}", "")
        # Computational Methods: Week 2 written up but dated in 2 days; Week 1 files-only.
        self.note(CM, "Week 1/Computational Methods Week 1.md", week_note("Computational Methods", 1, "files-only"))
        self.note(CM, "Week 2/Computational Methods Week 2.md",
                  week_note("Computational Methods", 2, extra="date: 2026-11-05"))
        self.note(CM, "Week 3/Computational Methods Week 3.md",
                  week_note("Computational Methods", 3, extra="date: 2026-11-10"))
        # Signals: everything written up (and one scaffolded) -> nothing.
        self.note(SS, "Week 1/Signals and Systems Week 1.md", week_note("Signals and Systems", 1))
        self.note(SS, "Week 2/Signals and Systems Week 2.md", week_note("Signals and Systems", 2, "scaffolded"))
        self.drafts = os.path.join(self.vault, "Denisse", "Drafts")

    def note(self, module, rel, text):
        p = os.path.join(self.vault, module, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def em_prep(self):
        return os.path.join(self.drafts, "2026-11-03 Electromagnetism Week 5 prep.md")


class PicksTheWeek(Fixture):
    def test_newest_files_only_week(self):
        results = dict((os.path.basename(p), s) for s, p in nl.run(self.vault, TODAY))
        self.assertEqual(results.get("2026-11-03 Electromagnetism Week 5 prep.md"), "wrote")
        self.assertNotIn("2026-11-03 Electromagnetism Week 4 prep.md", results)

    def test_dated_within_three_days_beats_older_files_only(self):
        results = [os.path.basename(p) for _, p in nl.run(self.vault, TODAY)]
        self.assertIn("2026-11-03 Computational Methods Week 2 prep.md", results)
        self.assertFalse(any("Computational Methods Week 3" in r for r in results), results)

    def test_written_up_and_scaffolded_weeks_do_not_qualify(self):
        results = [os.path.basename(p) for _, p in nl.run(self.vault, TODAY)]
        self.assertFalse(any("Signals" in r for r in results), results)

    def test_written_up_week_is_skipped_for_older_files_only(self):
        # Mark EM Week 5 written up: Week 4 is now the newest files-only.
        p = os.path.join(self.vault, EM, "Week 5", "Electromagnetism Week 5.md")
        with open(p, encoding="utf-8") as f:
            text = f.read()
        with open(p, "w", encoding="utf-8") as f:
            f.write(text.replace("status: files-only\n", ""))
        results = [os.path.basename(p) for _, p in nl.run(self.vault, TODAY)]
        self.assertIn("2026-11-03 Electromagnetism Week 4 prep.md", results)

    def test_date_outside_window_does_not_count(self):
        # 6 Nov: Week 2 (5 Nov) is past, Week 3 (10 Nov) is 4 days out -> files-only Week 1.
        later = TODAY + dt.timedelta(days=3)
        results = [os.path.basename(p) for _, p in nl.run(self.vault, later, apply=False)]
        self.assertIn("2026-11-06 Computational Methods Week 1 prep.md", results)
        self.assertFalse(any("Computational Methods Week 3" in r for r in results), results)

    def test_module_title(self):
        self.assertEqual(nl.module_title("5CCYB070 Computational Methods (26~27 SY)"), "Computational Methods")
        self.assertEqual(nl.module_title("5CCYB010 Electromagnetism"), "Electromagnetism")


class TheNote(Fixture):
    def setUp(self):
        super().setUp()
        nl.run(self.vault, TODAY)
        with open(self.em_prep(), encoding="utf-8") as f:
            self.text = f.read()

    def test_frontmatter(self):
        fm = nl._front(self.text)
        self.assertEqual(fm["name"], "2026-11-03 Electromagnetism Week 5 prep")
        self.assertEqual(fm["type"], "draft")
        self.assertEqual(fm["updated"], "2026-11-03")
        self.assertTrue(fm["description"])

    def test_topics_from_filenames_topic_and_concepts(self):
        topics = self.text.split("## Topics")[1].split("##")[0]
        bullets = [l for l in topics.splitlines() if l.startswith("- ")]
        self.assertTrue(3 <= len(bullets) <= 5, bullets)
        self.assertIn("Electromagnetic induction and curl", topics)
        self.assertIn("Induction motors", topics)
        self.assertIn("Lenz law", topics)
        self.assertIn("Faraday's law", topics)  # concept note citing Week 5
        # generic names and non-lecture/tutorial kinds are not topics
        self.assertNotIn("**Notes**", topics)
        self.assertNotIn("**Questions**", topics)
        self.assertNotIn("Sheet on flux", topics)

    def test_prior_week_todos(self):
        open_ = self.text.split("## Still open from Week 4")[1].split("##")[0]
        self.assertIn("Lecture write-up still to do", open_)
        self.assertIn("Check the tutorial numbering", open_)
        self.assertNotIn("Not a todo", open_)

    def test_two_earlier_concepts_by_relevance(self):
        earlier = self.text.split("## Earlier ideas to have ready")[1]
        bullets = [l for l in earlier.splitlines() if l.startswith("- ")]
        self.assertEqual(len(bullets), 2, bullets)
        self.assertIn("|Curl]]", earlier)  # shares "curl" with the week topic
        self.assertNotIn("Unrelated", earlier)
        self.assertNotIn("Faraday", earlier)  # this week's, not earlier

    def test_no_claims_beyond_filenames_and_notes(self):
        self.assertIn("Nothing here says what the lecture contains", self.text)
        fixture = []
        for p in glob.glob(os.path.join(self.vault, "Atlas", "**", "*"), recursive=True):
            fixture.append(os.path.basename(p))
            if p.endswith(".md"):
                with open(p, encoding="utf-8") as f:
                    fixture.append(f.read())
        corpus = "\n".join(fixture)
        topics = self.text.split("## Topics")[1].split("## Earlier")[0]
        for label in re.findall(r"^- \*\*(.+?)\*\*", topics, re.M) + re.findall(r"^- (.+?) — ", topics, re.M):
            self.assertIn(label.replace("**", ""), corpus, label)
        # every wikilink resolves to a fixture note or file
        names = {os.path.splitext(os.path.basename(p))[0].lower()
                 for p in glob.glob(os.path.join(self.vault, "**", "*"), recursive=True)}
        for target in nl._LINK.findall(self.text):
            target = target.strip()
            if "/" in target:
                self.assertTrue(os.path.exists(os.path.join(self.vault, target)), target)
            else:
                self.assertIn(target.lower(), names, target)

    def test_empty_sections_say_so_without_inventing(self):
        text = nl.build(self.vault, os.path.join(self.vault, CM), "Computational Methods",
                        *self._cm_week1())
        self.assertIn("There is no Week 0 note.", text)
        weeks = nl.read_weeks(os.path.join(self.vault, CM), "Computational Methods")
        text2 = nl.build(self.vault, os.path.join(self.vault, CM), "Computational Methods",
                         weeks[1], weeks, TODAY)
        self.assertIn("Nothing marked `[!todo]` in the Week 1 note.", text2)
        self.assertIn("No earlier concept section", text)

    def _cm_week1(self):
        weeks = nl.read_weeks(os.path.join(self.vault, CM), "Computational Methods")
        return weeks[0], weeks, TODAY


class NeverOverwrites(Fixture):
    def test_existing_note_left_alone(self):
        os.makedirs(self.drafts, exist_ok=True)
        with open(self.em_prep(), "w", encoding="utf-8") as f:
            f.write("his own edits\n")
        results = nl.run(self.vault, TODAY)
        with open(self.em_prep(), encoding="utf-8") as f:
            self.assertEqual(f.read(), "his own edits\n")
        self.assertIn(("exists", self.em_prep()), results)

    def test_one_note_per_module_week_across_dates(self):
        nl.run(self.vault, TODAY)
        results = nl.run(self.vault, TODAY + dt.timedelta(days=1))
        em = glob.glob(os.path.join(self.drafts, "* Electromagnetism Week 5 prep.md"))
        self.assertEqual(len(em), 1, em)
        self.assertTrue(all(s == "exists" for s, p in results if "Electromagnetism" in p), results)

    def test_dry_writes_nothing(self):
        results = nl.run(self.vault, TODAY, apply=False)
        self.assertTrue(results)
        self.assertFalse(os.path.exists(self.drafts))


class NightlyStep(Fixture):
    def test_silent_when_nothing_qualifies(self):
        lines = []
        empty = tempfile.mkdtemp(prefix="denisse-nextlec-empty-")
        self.addCleanup(shutil.rmtree, empty, True)
        self.assertEqual(nl.nightly_step(empty, lines.append), "none")
        self.assertEqual(lines, [])

    def test_silent_when_all_already_written(self):
        nl.run(self.vault, TODAY)
        lines = []
        self.assertEqual(nl.nightly_step(self.vault, lines.append, today=TODAY), "exists")
        self.assertEqual(lines, [])

    def test_reports_what_it_wrote(self):
        lines = []
        self.assertEqual(nl.nightly_step(self.vault, lines.append, today=TODAY), "wrote")
        self.assertTrue(any("Electromagnetism Week 5 prep.md" in l for l in lines), lines)

    def test_failure_is_logged_not_raised(self):
        real = nl.run

        def boom(*a, **k):
            raise OSError("disk on fire")
        nl.run = boom
        try:
            lines = []
            self.assertEqual(nl.nightly_step(self.vault, lines.append, today=TODAY), "failed")
        finally:
            nl.run = real
        self.assertTrue(any("disk on fire" in l for l in lines), lines)

    def test_nightly_calls_the_step_inside_a_try(self):
        """nightly.py runs everything at import, so check its shape instead:
        the prep stage is in a try whose handler catches Exception, and it
        comes before the commit stage."""
        with open(os.path.join(HERE, "nightly.py"), encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        guarded = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and "nightly_step" in ast.unparse(node.body[-1] if node.body else node):
                guarded = any(isinstance(h.type, ast.Name) and h.type.id == "Exception" for h in node.handlers)
        self.assertTrue(guarded, "next_lecture.nightly_step is not wrapped in try/except Exception")
        self.assertLess(src.index("nightly_step("), src.index('say("== commit ==")'))


class DifferentModuleNames(Fixture):
    def check_new_module(self, folder, title):
        module = os.path.join(nl.MODULES_REL, folder)
        self.note(module, f"{title} notes.md", f"[[{title} concepts]]")
        self.note(module, f"{title} concepts.md",
                  f"### Resonance\nObserved response.\n[[{title} Week 2]]\n"
                  f"### Field basics\nField relationships.\n[[{title} Week 1]]\n")
        self.note(module, f"Week 1/{title} Week 1.md", week_note(
            title, 1, body="> [!todo] Revisit the diagram\n"))
        self.note(module, f"Week 2/{title} Week 2.md", week_note(
            title, 2, "files-only").replace(f"{title} topic 2", "Field response"))
        self.note(module, "Week 2/Lecture - Field coupling.pdf", "")
        path = os.path.join(self.drafts, f"2026-11-03 {title} Week 2 prep.md")
        self.assertEqual(nl.module_title(folder), title)
        self.assertIn(("wrote", path), nl.run(self.vault, TODAY))
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("**Resonance**", text)
        self.assertIn("**Field coupling**", text)
        self.assertIn(f"[[{title} concepts#Field basics|Field basics]]", text)
        self.assertIn("Revisit the diagram", text)
        self.assertIn(("exists", path), nl.run(self.vault, TODAY + dt.timedelta(days=1)))

    def test_coded_module_with_year_suffix(self):
        self.check_new_module("PHY900 Planetary Science (2040~41)", "Planetary Science")

    def test_uncoded_module_with_accents_and_punctuation(self):
        self.check_new_module("Écologie & évolution", "Écologie & évolution")

    def test_punctuated_module_with_alternate_json_rules(self):
        # Load the unchanged script beside edited data, leaving nl's defaults intact.
        with open(os.path.join(HERE, "next_lecture.json"), encoding="utf-8") as f:
            config = json.load(f)
        config["paths"] = {"modules": ["Courses"], "drafts": ["Previews"]}
        config["patterns"].update({
            "module_code": r"^[A-Z]+-\d+$", "module_suffix": r"\s+@\[\d+\]$",
            "week_dir": r"^Session (\d+)$",
            "week_reference": r"\[\[{title} Block (\d+)",
            "todo": r"(?m)^>\s*\[!action\]\s*(.*?)\s*$",
            "concept_heading": r"(?m)^####\s+(.+?)\s*$",
            "section_boundary": r"(?m)^###\s", "generic_topic": r"(?i)^outline$",
        })
        config["naming"].update({
            "module_separator": "__", "week_note": "{title} Block {week}.md",
            "hub_note": "{title} index.md", "file_topic_separator": " -- ",
            "prep_stem": "{title} Block {week} preview.md",
        })
        config["week_rules"].update({
            "status_field": "stage", "pending_statuses": ["queued"],
            "date_fields": ["held_on", "fallback"], "soon_days": 6, "prior_week_offset": 2,
        })
        config["topics"] = {"metadata_field": "theme", "file_kinds": ["Seminar"]}
        config["concepts"].update({"name_suffixes": ["ideas"], "tag_fragments": ["knowledge"]})
        config["limits"] = {"topics": 2, "earlier": 1}
        for key, value in config["templates"].items():
            config["templates"][key] = ([line.replace("Week", "Block") for line in value]
                                         if isinstance(value, list) else value.replace("Week", "Block"))
        script_dir = os.path.join(self.vault, "isolated_script")
        os.makedirs(script_dir)
        script = shutil.copyfile(os.path.join(HERE, "next_lecture.py"),
                                 os.path.join(script_dir, "next_lecture.py"))
        with open(os.path.splitext(script)[0] + ".json", "w", encoding="utf-8") as f:
            json.dump(config, f)
        spec = importlib.util.spec_from_file_location("configured_next_lecture", script)
        configured = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "path", sys.path.copy()):
            spec.loader.exec_module(configured)

        title = "Adaptive (Bio)+ Systems"
        folder = f"BIO-42__{title} @[2040]"
        module = os.path.join("Courses", folder)
        self.assertEqual(configured.module_title(folder), title)
        self.note(module, f"{title} index.md", f"[[{title} ideas]]")
        self.note(module, f"{title} ideas.md",
                  f"#### Field basics\nField relationships.\n[[{title} Block 2]]\n"
                  f"#### Field models\nField approximations.\n[[{title} Block 2]]\n"
                  "#### Spurious\n[[Adaptive BioBio Systems Block 4]]\n"
                  f"### References\n[[{title} Block 4]]\n")
        self.note(module, f"Session 2/{title} Block 2.md",
                  "---\nstage: queued\n---\n> [!action] Review the previous diagram\n")
        self.note(module, f"Session 4/{title} Block 4.md",
                  "---\nheld_on: invalid\nfallback: 2026-11-09\ntheme: Field response\n---\n")
        self.note(module, f"Session 5/{title} Block 5.md", "---\nheld_on: 2026-11-10\n---\n")
        for name in ("Seminar -- Outline.pdf", "Seminar -- Field coupling.pdf",
                     "Seminar -- Resonance.pdf", "Lecture - Unwanted.pdf"):
            self.note(module, f"Session 4/{name}", "")
        path = os.path.join(self.vault, "Previews", f"2026-11-03 {title} Block 4 preview.md")
        self.assertEqual(configured.run(self.vault, TODAY, apply=False), [("would write", path)])
        self.assertFalse(os.path.exists(os.path.dirname(path)))
        self.assertEqual(configured.run(self.vault, TODAY), [("wrote", path)])
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("**Field response**", text)
        self.assertIn("**Field coupling**", text)
        for excluded in ("**Outline**", "**Resonance**", "Unwanted", "Spurious", "Week"):
            self.assertNotIn(excluded, text)
        self.assertIn("## Still open from Block 2", text)
        self.assertIn("Review the previous diagram", text)
        earlier = text.split("## Earlier ideas to have ready")[1]
        self.assertEqual(earlier.count("- [["), 1)
        self.assertIn(f"[[{title} ideas#Field basics|Field basics]]", earlier)
        sections = configured.concept_sections(os.path.join(self.vault, module, f"{title} ideas.md"), title)
        self.assertEqual(sections[-1]["weeks"], [])  # regex metacharacters stay literal
        self.assertEqual(configured.run(self.vault, TODAY), [("exists", path)])
        tomorrow_path = os.path.join(self.vault, "Previews", f"2026-11-04 {title} Block 5 preview.md")
        self.assertEqual(configured.run(self.vault, TODAY + dt.timedelta(days=1)),
                         [("wrote", tomorrow_path)])  # next block enters the configured window
        weeks = configured.read_weeks(os.path.join(self.vault, module), title)
        self.assertEqual(configured.pick_week(weeks, TODAY + dt.timedelta(days=8))["week"], 2)


if __name__ == "__main__":
    unittest.main()
