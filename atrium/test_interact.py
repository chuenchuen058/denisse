"""python -m unittest atrium.test_interact -v

The live page's interactions (atrium/interact.py, acted on by atrium/live_page/app.js): a lane
card carries what it opens to show, task rows carry their task id, the filter's segments and
the drawer's words, report fields and file patterns follow atrium/config.json, the drawer's
path is read-only and never reads outside .review. Temp .review and vault, a fixed clock."""
import json
import re
import unittest
from pathlib import Path
from urllib.parse import quote

from atrium import interact
from atrium.test_board import BOARD, BoardBase, fixture, section
from atrium.test_live import SHIPPED, tree, w

IX = BOARD["interactive"]
REPORT = """RESULT: PASS
GATE: checks -> pass; does not cover: the kerb
HUMAN: none
PREMISE: right
DONE:
- Repainted both halves
- Counted the stripes
NOT DONE:
- none
ODD:
- The paint was still wet
COMMITS: abc1234
"""


class InteractBase(BoardBase):
    def setUp(self):
        super().setUp()
        fixture(self.review, self.vault, self.now)
        day = self.now.date().isoformat()
        w(self.review, f"archive/{day}/t-pass.report.md", REPORT)
        st = json.loads((self.review / "state.json").read_text(encoding="utf-8"))
        st["tasks"]["t-pass"]["history"][0].update(
            model="model-x", runner_gate="pass: 2 command(s) exited 0",
            review={"verdict": "pass", "reasons": "Does what the task asks.", "lane": "codex"})
        w(self.review, "state.json", json.dumps(st))

    def detail(self, tid):
        return interact.task_detail(tid, self.now)


class PageHtmlTest(InteractBase):
    def test_a_lane_card_carries_what_it_opens_to_show(self):
        page, words = self.page(), IX["expand"]["words"]
        claude = section(page, "lane:claude")
        self.assertIn('<div class="more">', claude)
        self.assertIn(words["files"], claude)
        self.assertIn(words["log"], claude)
        self.assertIn("CONSULT: Don asked", claude)  # the runner's line about its task, without the id
        self.assertNotIn("t-run:", claude)
        self.assertIn(words["queued"], claude)
        self.assertIn('data-task="t-next"', claude)  # what is queued for the lane opens the drawer too
        copilot = section(page, "lane:copilot")  # idle: what it finished today
        self.assertIn(words["earlier"], copilot)
        self.assertIn("Quartz import tidy", copilot)
        self.assertNotIn(words["files"], copilot)

    def test_task_rows_carry_their_task_id(self):
        page = self.page()
        for key, tid in (("up_next", "t-next"), ("credits", "t-pass"), ("today", "t-run"), ("today", "t-partial"),
                         ("tree", "t-parked"), ("tree", "t-draft")):
            self.assertIn(f'data-task="{tid}"', section(page, key), (key, tid))
        self.assertNotIn("data-task", section(page, "activity"))

    def test_the_filter_segments_and_groups_follow_the_config(self):
        tr = section(self.page(), "tree")
        self.assertEqual(re.findall(r'data-seg="(\w+)"', tr), [s["key"] for s in IX["filter"]["segments"]])
        self.assertEqual(re.findall(r'data-group="(\w+)"', tr), ["you", "orchestra"])  # no reels section in this hot.md
        self.assertIn('id="q"', tr)

        def change(c):
            f = c["board"]["interactive"]["filter"]
            f["segments"] = [{"key": "everything", "label": "The lot", "groups": []},
                             {"key": "mine", "label": "Mine only", "groups": ["you"]}]
            f["quick"]["placeholder"] = "Find a task"
            del c["board"]["tree"]["groups"][0]["key"]  # no key: the group goes by its title
        self.edit(change)
        tr = section(self.page(), "tree")
        self.assertEqual(re.findall(r'data-seg="(\w+)"', tr), ["everything", "mine"])
        self.assertIn(">The lot</button>", tr)
        self.assertIn('placeholder="Find a task"', tr)
        self.assertIn('data-group="Waiting on you"', tr)

    def test_the_page_is_told_only_what_it_acts_on(self):
        ix = self.live.collect()["board"]["ix"]
        self.assertEqual(ix["vars"], IX["vars"])
        self.assertEqual(ix["keys"], IX["keys"])
        self.assertEqual(ix["path"], IX["drawer"]["path"])
        self.assertEqual(sorted(ix["words"]), sorted(IX["drawer"]["page_words"]))
        self.assertNotIn("files", json.dumps(ix["words"]))
        self.assertNotIn("archive", json.dumps(ix))  # no file pattern leaves the server


class DrawerTest(InteractBase):
    def test_a_finished_task_shows_its_run_report_and_reviewer(self):
        found, html = self.detail("t-pass")
        words = IX["drawer"]["words"]
        self.assertTrue(found)
        for text in ("Zebra crossing repaint", "Finished", "model-x", "$3.21", words["took"].format(m=30),
                     "pass: 2 command(s) exited 0", "Does what the task asks.", "Codex",
                     "<li>Repainted both halves</li>", "The paint was still wet", "abc1234", words["report"]):
            self.assertIn(text, html, text)
        self.assertIn('<span class="why ok">PASS</span>', html)
        self.assertNotIn("Not done", html)  # "none" is not a finding
        self.assertNotIn("Needs you", html)
        self.assertNotIn(">Premise<", html.split(words["report"])[1])  # read as a field, hidden in the report

    def test_parked_queued_and_running_tasks(self):
        words = IX["drawer"]["words"]
        found, html = self.detail("t-parked")
        self.assertTrue(found)
        self.assertIn("PARKED gate failed twice", html)
        self.assertIn(words["parked"], html)
        self.assertIn(words["no_run"], html)
        found, html = self.detail("t-next")
        self.assertTrue(found)
        self.assertIn("Otter holt census", html)
        self.assertIn(words["header"].format(name="Priority", value="urgent"), html)
        self.assertIn("Do the thing.", html)  # what the task asks
        found, html = self.detail("t-partial")
        self.assertIn(words["no_report"], html)  # it ran, and left no report file

    def test_unknown_and_unsafe_ids_find_nothing(self):
        w(self.tmp, "secret.md", "# Outside the review folder\n")
        w(self.review, "notes/elsewhere.md", "# Not a task\n")
        for tid in ("", "nope", "../secret", "..\\secret", "*", "t-*", "notes/elsewhere", "x" * 500, "t-pass.report"):
            found, html = self.detail(tid)
            self.assertFalse(found, tid)
            self.assertIn(IX["drawer"]["words"]["not_found"], html)
            self.assertNotIn("Outside", html)

    def test_words_fields_and_file_patterns_follow_the_config(self):
        w(self.review, "elsewhere/t-pass.txt", "SUMMARY: all of it\nRESULT: PASS\n")

        def change(c):
            d = c["board"]["interactive"]["drawer"]
            d["files"]["report"] = ["elsewhere/{id}.txt"]
            d["report"]["fields"] = [{"name": "SUMMARY", "label": "In short"}, {"name": "RESULT", "label": "R", "hide": True}]
            d["words"]["report"] = "What it said"
            d["run_rows"] = [{"label": "Who", "fields": ["agent"]}]
        self.edit(change)
        _, html = self.detail("t-pass")
        self.assertIn("<h3>What it said</h3><h4>In short</h4><p>all of it</p>", html)
        self.assertIn("<dt>Who</dt><dd>Claude</dd>", html)
        self.assertNotIn("Repainted", html)
        self.assertNotIn("model-x", html)

    def test_the_drawer_is_served_read_only(self):
        srv = self.serve()
        path = IX["drawer"]["path"]
        before = tree(self.review), tree(self.vault)
        code, ctype, body = self.get(srv, f"{path}?id=t-pass")
        self.assertEqual((code, ctype), (200, "application/json; charset=utf-8"))
        self.assertIn("Repainted both halves", json.loads(body)["html"])
        code, _, body = self.get(srv, f"{path}?id={quote('../secret', safe='')}")
        self.assertEqual(code, 404)
        self.assertIn(IX["drawer"]["words"]["not_found"], json.loads(body)["html"])
        self.assertEqual(self.get(srv, path)[0], 404)  # no id
        self.assertEqual(self.get(srv, f"{path}?id=t-pass", "POST")[0], 405)
        self.assertEqual(before, (tree(self.review), tree(self.vault)))


class ReportFieldsTest(unittest.TestCase):
    def test_fields_bullets_and_unknown_lines(self):
        got = interact.report_fields("intro\nRESULT: PASS\nDONE:\n- one\n  * two\nNOTE: kept with DONE\nNOT DONE:\n- three\n",
                                     ["RESULT", "DONE", "NOT DONE"])
        self.assertEqual(got, {"RESULT": ["PASS"], "DONE": ["one", "two", "NOTE: kept with DONE"], "NOT DONE": ["three"]})
        self.assertEqual(interact.report_fields("RESULT: PASS", []), {})


class PageAssetsTest(unittest.TestCase):
    """The page acts on the config's selectors, keys and durations; it names none itself."""

    def setUp(self):
        page = Path(__file__).resolve().parent / SHIPPED["live"]["page_dir"]
        self.css = (page / "style.css").read_text(encoding="utf-8")
        self.js = (page / "app.js").read_text(encoding="utf-8")
        self.index = (page / "index.html").read_text(encoding="utf-8")

    def test_no_word_key_or_duration_of_its_own(self):
        own = list(IX["drawer"]["words"].values()) + list(IX["expand"]["words"].values())
        own += [s["label"] for s in IX["filter"]["segments"]] + [IX["filter"]["quick"]["label"]]
        for text in own:
            if len(text) > 5:  # a real word or sentence, not a joiner or "All" (as in querySelectorAll)
                self.assertFalse(text in self.js or text in self.css, text)
        self.assertNotIn(IX["drawer"]["path"].strip("/") + "?", self.js)  # the path comes from the data
        self.assertNotIn('"Escape"', self.js)
        # every duration is a --ix- variable (a bare 0s, "at once", is not one)
        self.assertIsNone(re.search(r"[1-9]\d*m?s\b", self.css.split("/* Interactions")[1]))
        for needle in ("ix.vars", "ix.keys", "ix.tips", "ix.expand", "ix.rows", "ix.filter", "ix.count", "ix.path"):
            self.assertIn(needle, self.js.replace("D.board.", ""), needle)

    def test_motion_is_optional_and_the_drawer_is_on_the_page(self):
        self.assertIn("prefers-reduced-motion: reduce", self.js)
        self.assertIn("@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}", self.css)
        self.assertIn('id="drawer"', self.index)
        self.assertIn('id="scrim"', self.index)


if __name__ == "__main__":
    unittest.main(verbosity=2)
