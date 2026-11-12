"""python -m unittest atrium.test_board -v

The v3 mission board (atrium/board.py, shown by atrium/live_page/): the page is rendered from a
fixture .review and vault and checked section by section; nothing from the reference mock
(atrium/design/mission-board-reference.html, sample data) may appear on it; an empty .review
gives empty states and no numbers; words, colours and lanes follow atrium/config.json.
Temp .review, temp vault, a fixed clock; no Ollama, no GPU, no network."""
import copy
import datetime as dt
import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from atrium import board
from atrium.test_live import SHIPPED, LiveBase, task, w

HERE = Path(__file__).resolve().parent
MOCK = HERE / "design" / "mission-board-reference.html"
BOARD = SHIPPED["live"]["board"]
BARE_COUNT = re.compile(r"^[0-9]+( of [0-9]+)?$")


class Text(HTMLParser):
    """The text a reader sees: every text node outside <script>/<style>, plus the <title>."""

    def __init__(self):
        super().__init__()
        self.chunks, self.sections, self._skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        self._skip += tag in ("script", "style")
        self.sections += [v for k, v in attrs if k == "data-section"]

    def handle_endtag(self, tag):
        self._skip -= tag in ("script", "style")

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.chunks.append(" ".join(data.split()))


def read(html_text: str) -> Text:
    p = Text()
    p.feed(html_text)
    return p


def strings(node) -> set[str]:
    """Every string in a config subtree, lower-cased: the page's own labels."""
    if isinstance(node, str):
        return {node.strip().lower()}
    if isinstance(node, dict):
        return set().union(*[strings(v) for k, v in node.items() if not k.startswith("_")] or [set()])
    if isinstance(node, list):
        return set().union(*[strings(v) for v in node] or [set()])
    return set()


def section(page: str, key: str) -> str:
    """One card's HTML, by its data-key."""
    m = re.search(rf'<div class="w [^"]*" data-key="{re.escape(key)}".*?(?=<div class="w |</div></body>)', page, re.S)
    return m.group(0) if m else ""


def fixture(review: Path, vault: Path, now: dt.datetime) -> None:
    """One of everything the board reads: finished runs with tokens, a running task with its
    START line, a merge, a consult, a limited lane, a queue, a draft, a parked task, the exam
    and digest state files, and a hot.md with open, done and Orchestra-owned lines."""
    day = now.date().isoformat()
    at = lambda h, m=0: f"{day}T{h:02d}:{m:02d}:00"
    started = (now - dt.timedelta(minutes=20)).isoformat(timespec="seconds")
    state = {"tasks": {
        "t-pass": {"history": [{"end": at(9, 30), "seconds": 1800, "result": "PASS", "lane": "claude", "spent": "$3.21",
                                "tokens": {"in": 1200, "out": 34000, "cache_read": 7000000, "cache_write": 150000}}]},
        "t-partial": {"history": [{"end": at(10), "seconds": 600, "result": "PARTIAL", "lane": "copilot",
                                   "spent": "unknown", "tokens": None}]},
        "t-old": {"history": [{"end": "2026-05-02T08:00:00", "seconds": 60, "result": "PASS", "lane": "claude",
                               "spent": "$0.10", "tokens": {"in": 100, "out": 900, "cache_read": 0, "cache_write": 0}}]},
        "t-run": {"history": [], "last_start": started}},
        "days": {day: {"cost_usd": 17.4, "work_s": 2400}},
        "lanes": {"codex": {"limited_until": (now + dt.timedelta(hours=3)).isoformat(timespec="seconds"),
                            "since": (now - dt.timedelta(hours=1)).isoformat(timespec="seconds")},
                  "copilot": {"limited_until": (now + dt.timedelta(hours=5)).isoformat(timespec="seconds")}}}
    w(review, "state.json", json.dumps(state))
    w(review, "campaign.json", json.dumps({"enabled": True, "overrides": {
        "daily_spend_cap_usd": 58, "lane_slots": {"claude": 2, "codex": 1}}}))
    w(review, f"archive/{day}/t-pass.md", task("Zebra crossing repaint"))
    w(review, f"archive/{day}/t-partial.md", task("Quartz import tidy", lane="copilot"))
    w(review, "queue/t-run.md", task("Heron nest survey"))
    w(review, "queue/t-next.md", task("Otter holt census", priority="urgent"))
    w(review, "queue/t-codex.md", task("Lynx den audit", lane="codex"))
    w(review, "queue/t-draft.md", task("Vole run sketch", queue="draft"))
    w(review, "parked/t-parked.md", task("Badger sett repair"))
    w(review, "parked/t-parked.why.txt", "PARKED gate failed twice\n")
    w(review, "runner.claude.lock", json.dumps({"task": "t-run", "since": started}))
    w(review, "runner.log", "\n".join([
        f"{at(9)} START t-pass lane=claude weight=light timeout=1200s attempt=1",
        f"{at(9, 30)} PASS t-pass after 1800s (attempt 1, $3.21) prompt abc tokens in 1200 out 34000",
        f"{at(9, 31)} merge review/t-pass: merged",
        f"{at(9, 32)} hot.md waiting list updated (3 item(s))",
        f"{at(10, 5)} LIMITED t-codex lane=codex until 15:00",
        f"{at(11, 40)} START t-run lane=claude weight=heavy timeout=2700s attempt=1",
        f"{at(11, 41)} CONSULT t-run: Don asked (every task) path=model -> high confidence by local after 29s",
    ]) + "\n")
    w(review, "exams/last_run.json", json.dumps({"finished": at(8, 15), "results": {
        "codex": "1/1", "local:modelx:7b": "3/4", "local:modely:9b": "2/2"}}))
    w(vault, SHIPPED["digest"]["state_file"], json.dumps({"last": at(11, 2)}))
    w(vault, "hot.md", "\n".join([
        "## Active right now", "- 2026-06-14 [Chuen] Kestrel box build is under way", "- TODO not shown",
        "## Waiting on Chuen", "- Sign the heron permit", "- ~~Order the otter tags~~ done",
        "<!-- orchestra:waiting -->", "- Approve draft task \"Vole run sketch\"", "<!-- /orchestra:waiting -->", ""]))


class BoardBase(LiveBase):
    def setUp(self):
        super().setUp()
        board._cache.clear()
        self.addCleanup(board._cache.clear)
        for name, value in (("_gpu_line", ""), ("_ollama", None)):
            p = mock.patch.object(board, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(board, "dt", self.live.dt)  # the same fixed clock as live.py
        p.start()
        self.addCleanup(p.stop)

    def page(self) -> str:
        return board.render(self.live.collect())

    def edit(self, change) -> None:
        cfg = json.loads((self.tmp / "atrium.json").read_text(encoding="utf-8"))
        change(cfg["live"])
        (self.tmp / "atrium.json").write_text(json.dumps(cfg), encoding="utf-8")


class BoardFixtureTest(BoardBase):
    def setUp(self):
        super().setUp()
        fixture(self.review, self.vault, self.now)
        for name, value in (("_gpu_line", "37, 5120, 8192"),
                            ("_ollama", {"models": [{"name": "modelx:7b", "vram": 3 * 1073741824}]})):
            p = mock.patch.object(board, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_all_twelve_sections_are_on_the_page_with_real_data(self):
        # the shipped all-time start (1 Sep 2026) is after this fixture's clock; start it at the oldest run here
        cfg = copy.deepcopy(board._b())
        cfg["tokens"]["all_from"] = "2026-05-01"
        p = mock.patch.object(board, "_b", return_value=cfg)
        p.start()
        self.addCleanup(p.stop)
        page = self.page()
        seen = read(page)
        words, lw = BOARD["words"], SHIPPED["live"]["words"]
        # 1 title (page title and big heading); the favicon is built from the agents and the verdict
        self.assertIn(f"<title>{BOARD['title']}</title>", page)
        self.assertIn(f'<h1 data-section="title">{BOARD["title"]}</h1>', page)
        d = self.live.collect()
        fav = d["board"]["favicon"]
        self.assertEqual(fav["arcs"], [a["colour"] for a in SHIPPED["live"]["agents"]])
        self.assertEqual(fav["dot"], BOARD["theme"][BOARD["favicon"]["scheme"]]["warn"])  # two lanes are limited
        # 2 header: the verdict line and the pill with working/stuck counts and battery
        self.assertIn("header", seen.sections)
        until = (self.now + dt.timedelta(hours=3)).strftime(words["verdict_time"])
        later = (self.now + dt.timedelta(hours=5)).strftime(words["verdict_time"])
        self.assertEqual(d["board"]["verdict"], {  # the first limited lane, in the config's order
            "level": "warn", "text": words["verdict_limited"].format(label="Copilot", time=later),
            "pill": words["pill"].format(working=1, stuck=0) + words["pill_power"].format(icon=lw["battery"], pct=64)})
        self.assertIn(d["board"]["verdict"]["text"], seen.chunks)
        self.assertIn(d["board"]["verdict"]["pill"], seen.chunks)
        self.assertIn('id="state"', page)  # Live / Reconnecting goes here
        # 3 four rings: pass rate, slots, agents ready, spend against the cap
        self.assertEqual(re.findall(r'data-ring="(\w+)"', page), [r["key"] for r in BOARD["rings"]])
        rings = dict(zip([r["key"] for r in BOARD["rings"]], re.findall(r'<div class="v">(.*?)</div>', page)))
        self.assertEqual(rings, {"pass": "50%", "slots": "1 of 3", "ready": "1 of 4", "spend": "$17"})
        self.assertIn("pass rate today · 1 of 2", seen.chunks)
        self.assertIn("of $58 today · notional", seen.chunks)
        # 4 one card per configured lane
        self.assertEqual(seen.sections.count("lane"), len(SHIPPED["live"]["agents"]))
        claude = section(page, "lane:claude")
        self.assertIn(f'>{words["state_working"]}', claude)
        self.assertIn("Heron nest survey", claude)
        self.assertIn('data-timeout-s="2700"', claude)  # the timeout the runner logged at START
        start_h = float(re.search(r'data-start-h="([\d.]+)"', claude).group(1))
        self.assertAlmostEqual(start_h, 12 - 20 / 60, places=2)
        self.assertIn(words["slots"].format(busy=1, slots=2), claude)
        self.assertIn(lw["queued"].format(n=1), claude)
        codex = section(page, "lane:codex")
        self.assertIn(f'>{words["state_limited"]}', codex)
        self.assertIn(words["resets"].format(time=until), codex)
        self.assertIn(words["resets"].format(time=later), section(page, "lane:copilot"))
        self.assertIn(f'>{words["state_down"]}', section(page, "lane:local"))  # no Ollama in the usage table
        # 5 who's editing what: the running task's pointer, in its agent's colour
        cur = section(page, "cursors")
        self.assertIn('data-seat="claude:1"', cur)
        self.assertIn(SHIPPED["live"]["agents"][0]["colour"], cur)
        self.assertIn('class="layer"', cur)
        # 6 merged today, needs a look, up next
        self.assertIn('<div class="big">1</div>', section(page, "merged"))
        self.assertIn(words["merged_of"].format(n=2), section(page, "merged"))
        look = section(page, "needs_look")
        self.assertIn('<div class="big">1</div>', look)
        self.assertIn(words["needs_sub"].format(parked=1, drafts=1), look)
        self.assertIn(" alert", look)
        nxt = section(page, "up_next")
        self.assertLess(nxt.index("Otter holt census"), nxt.index("Lynx den audit"))  # urgent first
        self.assertNotIn("Heron nest survey", nxt)  # running, not next
        self.assertIn(lw["why_limited"], nxt)
        # 7 what to work on: hot.md and the queue
        tr = section(page, "tree")
        for text in ("Sign the heron permit", "Order the otter tags", "Otter holt census", "Badger sett repair",
                     "Vole run sketch"):
            self.assertIn(text, tr)
        self.assertEqual(tr.count("Vole run sketch"), 1)  # Orchestra's own block in hot.md is not repeated
        self.assertIn('class="nd d"><i></i>Order the otter tags', tr)
        self.assertIn('class="nd u"><i></i>Otter holt census', tr)
        # 8 local agents: Ollama, GPU, Don, local evals, digest
        loc = section(page, "local")
        lo = BOARD["local"]["words"]
        for text in (lo["ollama_up"], "modelx:7b", lo["vram"].format(used=3.0), lo["gpu_busy"].format(pct=37),
                     lo["gpu_mem"].format(used=5.0, total=8.0), lo["consults"].format(n=1),
                     lo["last_consult"].format(time="11:41", confidence="high"), lo["evals"].format(v=5, m=6),
                     "modely:9b 2/2", lo["evals_last"].format(time="08:15"), lo["digest_last"].format(time="11:02")):
            self.assertIn(text, loc)
        self.assertNotIn("codex 1/1", loc)  # not a local model
        # 9 tokens spent and credits per task
        tok = section(page, "tokens")
        self.assertEqual(re.findall(r'<div class="big mid">(.*?)</div>', tok), ["7.2M", "7.2M", "7.2M", "7.2M"])
        self.assertIn("today · 35.2k fresh", tok)
        self.assertIn("all time · since 01 May (data from 02 May)", tok)
        self.assertIn("Claude 7.2M", tok)
        self.assertIn("Copilot n/a", tok)  # reports no tokens: unknown, not 0
        cr = section(page, "credits")
        self.assertIn("Zebra crossing repaint", cr)
        self.assertIn("Claude · 7.2M tokens · $3.21", cr)
        self.assertIn(BOARD["credits"]["row_no_tokens"].format(label="Copilot", spent=BOARD["credits"]["spent_unknown"]), cr)
        # 10 limits and resets, from the runner's lane record
        lim = section(page, "limits")
        self.assertIn(words["limit_until"].format(time=until), lim)
        self.assertIn("width:25%", lim)  # an hour into a four-hour limit
        self.assertIn(words["limit_until"].format(time=later), lim)
        self.assertEqual(lim.count("width:0%"), len(SHIPPED["live"]["agents"]) - 1)  # no "since": no bar invented
        self.assertEqual(lim.count(words["limit_none"]), len(SHIPPED["live"]["agents"]) - 2)
        # 11 live activity: runner.log events, newest first, real ones only
        act = re.findall(r"<li><span class=\"t\">(.*?)</span><span>(.*?)</span></li>", section(page, "activity"))
        self.assertEqual(act, [("11:41", "Don gave an opinion (high confidence): Heron nest survey"),
                               ("11:40", "Claude started: Heron nest survey"),
                               ("10:05", "Codex limited until 15:00"),
                               ("09:31", "✓ Merged: Zebra crossing repaint"),
                               ("09:30", "PASS: Zebra crossing repaint"),
                               ("09:00", "Claude started: Zebra crossing repaint")])
        # 12 today and projects
        today = section(page, "today")
        self.assertLess(today.index("Heron nest survey"), today.index("Quartz import tidy"))
        self.assertIn(words["result_running"], today)
        self.assertIn("<b>09:00</b>", today)
        proj = section(page, "projects")
        self.assertIn("<b>Chuen</b><span>Kestrel box build is under way</span>", proj)
        self.assertNotIn("not shown", proj)
        for name in ("pill", "rings", "cursors", "merged", "needs_look", "up_next", "tree", "local", "tokens",
                     "credits", "limits", "activity", "today", "projects"):
            self.assertIn(name, seen.sections)

    def test_no_sample_text_from_the_mock_is_on_the_page(self):
        sample, own = read(MOCK.read_text(encoding="utf-8")), strings(SHIPPED["live"])
        self.assertGreater(len(sample.chunks), 150)  # the mock was read
        for page in (self.page(), self._empty_page()):
            seen = read(page)
            chunks = set(seen.chunks)
            for chunk in sample.chunks:
                if chunk.lower() in own:  # a label the config names (a heading, a lane, a state)
                    continue
                if BARE_COUNT.match(chunk):  # "5", "3 of 4": a real count can equal a sample one
                    continue
                self.assertNotIn(chunk, chunks)
                if len(chunk) >= 12:  # nor as part of a longer line
                    self.assertEqual([c for c in chunks if chunk in c], [])

    def _empty_page(self) -> str:
        import shutil
        shutil.rmtree(self.review)
        shutil.rmtree(self.vault)
        board._cache.clear()
        with mock.patch.object(board, "_gpu_line", return_value=""), \
                mock.patch.object(board, "_ollama", return_value=None):
            return self.page()

    def test_a_task_past_its_timeout_is_stuck(self):
        started = (self.now - dt.timedelta(minutes=50)).isoformat(timespec="seconds")  # timeout=2700s = 45 min
        st = json.loads((self.review / "state.json").read_text(encoding="utf-8"))
        st["tasks"]["t-run"]["last_start"] = started
        w(self.review, "state.json", json.dumps(st))
        v = self.live.collect()["board"]
        self.assertEqual(v["verdict"]["level"], "bad")
        self.assertEqual(v["verdict"]["text"], BOARD["words"]["verdict_stuck"].format(n=1))
        self.assertEqual(v["favicon"]["dot"], BOARD["theme"][BOARD["favicon"]["scheme"]]["bad"])

    def test_words_colours_cards_and_lanes_follow_the_config(self):
        def change(c):
            c["agents"] = [{"lane": "codex", "label": "Cx", "colour": "#123456"}]
            b = c["board"]
            b["title"] = "Control room"
            b["headings"]["tree"] = "Do this"
            b["words"]["state_limited"] = "Resting"
            b["words"]["empty"] = "nothing here"
            b["theme"]["light"]["warn"] = "#abcdef"
            b["cards"] = [x for x in b["cards"] if x["id"] != "local"]
            b["rings"] = b["rings"][:1]
            b["activity"]["events"] = [{"pattern": "^merge review/(?P<task>[^:]+): merged", "text": "landed {title}"}]
        self.edit(change)
        d = self.live.collect()
        page = board.render(d)
        seen = read(page)
        self.assertIn('<h1 data-section="title">Control room</h1>', page)
        self.assertEqual(seen.sections.count("lane"), 1)
        self.assertIn(">Resting", section(page, "lane:codex"))
        self.assertIn("--c:#123456", page)
        self.assertNotIn("lane:claude", page)
        self.assertIn("<h3>Do this</h3>", page)
        self.assertNotIn("local", seen.sections)
        self.assertEqual(len(re.findall("data-ring=", page)), 1)
        self.assertEqual(d["board"]["favicon"]["arcs"], ["#123456"])
        self.assertEqual(d["board"]["favicon"]["dot"], "#abcdef")
        self.assertEqual(d["board"]["theme"]["light"]["warn"], "#abcdef")
        self.assertIn("landed Zebra crossing repaint", section(page, "activity"))
        self.assertNotIn("started:", section(page, "activity"))

    def test_the_board_is_served_and_json(self):
        srv = self.serve()
        code, _, body = self.get(srv, "/data")
        self.assertEqual(code, 200)
        b = json.loads(body)["board"]
        self.assertEqual(b["title"], BOARD["title"])
        self.assertEqual([c["key"] for c in b["cards"]][:len(SHIPPED["live"]["agents"])],
                         [f"lane:{a['lane']}" for a in SHIPPED["live"]["agents"]])
        cursors = next(c for c in b["cards"] if c["key"] == "cursors")
        self.assertIn(cursors["rows"], cursors["html"])  # app.js keeps the pointer layer, swaps the rows


class BoardEmptyTest(BoardBase):
    """Nothing to read anywhere: every section is still there, shows its empty state, and no
    figure is invented."""

    def test_empty_review_and_vault_give_empty_states_not_numbers(self):
        page = self.page()
        seen = read(page)
        words, lw = BOARD["words"], SHIPPED["live"]["words"]
        for key in ("tree", "up_next", "tokens", "credits", "activity", "today", "projects", "markets"):
            self.assertIn(f'<div class="none">{words["empty"]}</div>', section(page, key), key)
            self.assertIsNone(re.search(r">[^<]*\d[^<]*<", section(page, key)), key)
        self.assertIn(lw["nobody_working"], section(page, "cursors"))
        self.assertEqual(seen.sections.count("lane"), len(SHIPPED["live"]["agents"]))
        self.assertIn(f'>{words["state_free"]}', section(page, "lane:claude"))
        self.assertIn(words["no_task"], section(page, "lane:claude"))
        rings = dict(zip([r["key"] for r in BOARD["rings"]], re.findall(r'<div class="v">(.*?)</div>', page)))
        self.assertEqual(rings["pass"], words["dash"])
        self.assertIn(BOARD["rings"][0]["empty"], seen.chunks)
        self.assertNotIn("stroke-dasharray", page.split('id="grid"')[0].split('data-ring="slots"')[0])  # no arc drawn
        loc = section(page, "local")
        lo = BOARD["local"]["words"]
        self.assertIn(lo["ollama_down"], loc)
        self.assertIn(lo["no_consults"], loc)
        self.assertEqual(loc.count(f'<div class="s">{words["empty"]}</div>'), 3)  # GPU, evals, digest
        self.assertIsNone(re.search(r">[^<]*\d[^<]*<", loc))
        self.assertEqual(section(page, "limits").count(words["limit_none"]), len(SHIPPED["live"]["agents"]))
        self.assertIn('<div class="big">0</div>', section(page, "merged"))  # a count of nothing is 0
        self.assertIn(words["needs_none"], section(page, "needs_look"))
        v = self.live.collect()["board"]["verdict"]
        self.assertEqual((v["level"], v["text"]), ("ok", words["verdict_ok"]))
        json.dumps(self.live.collect())

    def test_garbled_sources_are_an_empty_board_not_an_error(self):
        w(self.review, "state.json", json.dumps({"tasks": {"t": {"history": [{"end": "soon", "tokens": "many"}]}},
                                                 "lanes": {"codex": "limited"}}))
        w(self.review, "runner.log", "not a log line\n\n2026-06-15T09:00:00\nxx START\n")
        w(self.review, "exams/last_run.json", "{half")
        w(self.vault, SHIPPED["digest"]["state_file"], "[1, 2]")
        page = self.page()
        self.assertIn(f'<div class="none">{BOARD["words"]["empty"]}</div>', section(page, "tokens"))
        self.assertIn(f'<div class="none">{BOARD["words"]["empty"]}</div>', section(page, "activity"))


class PageAssetsTest(unittest.TestCase):
    """The static page: no colour, word or sample data of its own; the stream, the Live state,
    the tab-hidden handling, the theme button and the pointer glide are all still there."""

    def setUp(self):
        page = HERE / SHIPPED["live"]["page_dir"]
        self.css = (page / "style.css").read_text(encoding="utf-8")
        self.js = (page / "app.js").read_text(encoding="utf-8")
        self.index = (page / "index.html").read_text(encoding="utf-8")

    def test_no_colour_is_written_in_the_page_files(self):
        for name, text in (("style.css", self.css), ("app.js", self.js), ("index.html", self.index)):
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b(?![\w-])|rgba?\(|hsla?\(", text), name)

    def test_the_page_has_no_text_of_its_own(self):
        self.assertEqual(read(self.index).chunks, [])
        self.assertNotIn("FALLBACK", self.js)
        for lane in (a["lane"] for a in SHIPPED["live"]["agents"]):
            self.assertIsNone(re.search(rf"\b{lane}\b", self.js, re.I), lane)
        self.assertNotIn("--agent-", self.css)  # an agent's colour arrives inline, from the config
        for label in strings(BOARD["headings"]) | strings(BOARD["words"]["verdict_ok"]):
            self.assertNotIn(label, self.js.lower())

    def test_stream_state_theme_and_glide_are_kept(self):
        for needle in ('new EventSource("events")', "visibilitychange", "es.close()", '$("state")', "D.words.live",
                       "D.words.stale", "theme_storage_key", ".layer", "translate(", "favicon", "data-timer"):
            self.assertIn(needle, self.js, needle)
        self.assertIn(".layer .cur{position:absolute;left:0;top:0;transition:transform var(--cur-glide)", self.css)
        self.assertIn('id="favicon"', self.index)


if __name__ == "__main__":
    unittest.main(verbosity=2)
