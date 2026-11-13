"""python -m unittest atrium.test_suggest -v

The Do next card (atrium/suggest.py) against fixture notes in a temp vault and a temp .review:
his Saturday rule, the rules ranking when the model can't answer, bad model JSON, the
never-rules, done/snooze persistence, and the hot.md strike only on an exact match.
No network, no Ollama: every model answer is an injected function."""
import contextlib
import copy
import datetime as dt
import http.client
import importlib
import io
import json
import os
import shutil
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from atrium import conf

SHIPPED = json.loads(conf.DEFAULT.read_text(encoding="utf-8"))
FRI = dt.datetime(2026, 11, 13, 10, 0)   # a Friday, outside the study slots
SAT = dt.datetime(2026, 11, 14, 10, 0)

HOT = ("## Start here\n- read me\n\n## Waiting on Chuen\n"
       "- Renew the library books\n"
       "- Mock: Day 1 not finished, Q1 open\n"
       "- Hackathon form: apply by 15 Nov\n"
       "- ~~Old thing~~ (done 1 Nov)\n"
       "- Reply to Dr Example about the start date\n"
       "<!-- orchestra:waiting -->\n- Parked \"something\": generated\n<!-- /orchestra:waiting -->\n"
       "\n## Open hazards\n- not a task\n")


def w(root: Path, rel: str, text: str, newline: str = "\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return p


def tree(root: Path) -> dict:
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(root.rglob("*"))}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.vault, self.review = self.tmp / "vault", self.tmp / "review"
        self.vault.mkdir(), self.review.mkdir()
        cfg = copy.deepcopy(SHIPPED)
        for src in cfg["suggest"]["sources"]:
            src["root"] = "vault"  # the plan file too: everything in the fixture vault
        cfg["suggest"]["orchestra"]["use"] = False
        self.cfg = cfg
        self._write_cfg()
        p = mock.patch.dict(os.environ, {"DENISSE_VAULT": str(self.vault), "ATRIUM_CONFIG": str(self.tmp / "a.json")})
        p.start()
        self.addCleanup(p.stop)
        from atrium import context, snapshot, suggest
        self.addCleanup(lambda: (importlib.reload(context), importlib.reload(snapshot)))
        importlib.reload(context)
        self.snap = importlib.reload(snapshot)
        self.snap.REVIEW = self.review
        self.s = importlib.reload(suggest)
        self.hot = w(self.vault, "hot.md", HOT)
        w(self.vault, "Atlas/Academic/Don/Don - Mod A progress.md",
          "---\nname: x\n---\n# Mod A\n\n## Anki reviews\n\n- Last read on 2026-11-08: 40 card(s), 0 reviewed.\n")
        w(self.vault, "GitHub/robotic-tail/docs/plan.md",
          "# Plan\n\n## Phase 0: unblock (1-5 Nov)\n- old\n\n## Phase 1: build (16-22 Nov)\n- **Gate:** parts ordered\n"
          "\n## Phase 2: test (23 Nov - 6 Dec)\n- later\n")

    def _write_cfg(self):
        (self.tmp / "a.json").write_text(json.dumps(self.cfg), encoding="utf-8")

    def facts(self, now=FRI):
        return self.s.gather(now, [])

    def by_title(self, facts, start):
        return next(f for f in facts if f["title"].startswith(start))


class GatherTest(Base):
    def test_reads_the_waiting_list_notes_and_the_next_plan_phase(self):
        f = self.facts()
        titles = [x["title"] for x in f]
        self.assertIn("Renew the library books", titles)
        self.assertNotIn("Old thing", " ".join(titles))  # struck already
        self.assertFalse(any("generated" in t for t in titles))  # Orchestra's block in hot.md
        self.assertFalse(any("not a task" in t for t in titles))  # another section
        self.assertIn("Study: Mod A", titles)
        self.assertIn("Robotic tail: Phase 1: build (16-22 Nov)", titles)  # phase 0 is past; one phase only
        self.assertFalse(any("Phase 2" in t for t in titles))
        hack = self.by_title(f, "Hackathon")
        self.assertEqual(hack["deadline"], dt.date(2026, 11, 15))
        self.assertTrue(self.by_title(f, "Mock")["behind"])
        self.assertTrue(self.by_title(f, "Study: Mod A")["behind"])
        self.assertEqual(self.by_title(f, "Renew")["line"], "- Renew the library books")
        self.assertEqual(self.by_title(f, "Study")["line"], "")  # only hot.md lines are strikable

    def test_ids_are_stable_across_reads(self):
        self.assertEqual([x["id"] for x in self.facts()], [x["id"] for x in self.facts()])

    def test_orchestra_needs_are_facts(self):
        self.cfg["suggest"]["orchestra"]["use"] = True
        self._write_cfg()
        f = self.s.gather(FRI, [{"item": "Approve draft: X", "kind": "draft"}])
        self.assertEqual(f[-1]["source"], "orchestra")
        self.assertEqual(f[-1]["title"], "Approve draft: X")


class RulesTest(Base):
    def test_deadline_first_then_behind_then_oldest(self):
        r = self.s.rules(self.facts(), FRI)
        titles = [x["title"] for x in r]
        self.assertEqual(titles[0], "Hackathon form: apply by 15 Nov")
        self.assertTrue(titles[1].startswith("Robotic tail: Phase 1"))  # due 22 Nov
        behind = [t for t in titles[2:5]]
        self.assertEqual(behind, ["Mock: Day 1 not finished, Q1 open", "Study: Mod A", "Renew the library books"])

    def test_in_a_study_slot_study_comes_before_other_undated_items(self):
        slot = dt.datetime.combine(FRI.date(), dt.time.fromisoformat(SHIPPED["suggest"]["study_slots"][0]["from"]))
        r = self.s.rules(self.facts(slot), slot)
        undated = [x for x in r if x["title"] not in ("Hackathon form: apply by 15 Nov",)
                   and not x["title"].startswith("Robotic")]
        self.assertEqual(undated[0]["kind"], "study")

    def test_model_down_gives_rules_and_the_label(self):
        def down(*a):
            raise TimeoutError("lease held by another job")
        d = self.s.current(FRI, [], ask=down, wait=True)
        self.assertEqual(d["mode"], "rules")
        self.assertEqual(len(d["items"]), 3)
        self.assertIn(SHIPPED["suggest"]["words"]["mode_rules"], self.s.card(d))

    def test_model_not_up_is_never_asked(self):
        ask = mock.Mock(side_effect=AssertionError("asked"))
        d = self.s.current(FRI, [], model_up=False, ask=ask, wait=True)
        ask.assert_not_called()
        self.assertEqual(d["mode"], "rules")


class ModelTest(Base):
    def reply(self, items):
        return lambda text, c: json.dumps({"items": items})

    def item(self, f, **kw):
        return dict({"title": "Do it", "why": "because", "minutes": 20, "source": f["id"], "kind": "admin"}, **kw)

    def test_good_json_is_used_and_validated(self):
        f = self.facts()
        renew = self.by_title(f, "Renew")
        d = self.s.current(FRI, [], ask=self.reply([self.item(renew, minutes=9999)]), wait=True)
        self.assertEqual(d["mode"], "model")
        self.assertEqual(d["items"][0]["title"], "Do it")
        self.assertEqual(d["items"][0]["minutes"], SHIPPED["suggest"]["minutes"]["max"])
        self.assertEqual(len(d["items"]), 3)  # the model gave one: the rules fill the rest
        self.assertEqual(len({i["id"] for i in d["items"]}), 3)

    def test_the_prompt_carries_his_rules_and_the_fact_ids(self):
        seen = {}

        def ask(text, c):
            seen["t"] = text
            raise ValueError("no")
        f = self.facts()
        self.s.rank(f, FRI, ask)
        for x in f:
            self.assertIn(x["id"], seen["t"])
        self.assertIn("Friday", seen["t"])

    def test_bad_json_falls_back_to_rules(self):
        f = self.facts()
        good = self.item(f[0])
        bad = ["not json at all", json.dumps({"items": []}), json.dumps([good]),
               json.dumps({"items": [dict(good, source="made-up")]}),
               json.dumps({"items": [dict(good, kind="gossip")]}),
               json.dumps({"items": [dict(good, minutes="ten")]}),
               json.dumps({"items": [dict(good, minutes=True)]}),
               json.dumps({"items": [{k: v for k, v in good.items() if k != "why"}]}),
               json.dumps({"items": [good, "x"]})]
        for reply in bad:
            mode, ranked = self.s.rank(f, FRI, lambda t, c, r=reply: r)
            self.assertEqual(mode, "rules", reply)
            self.assertEqual(ranked, self.s.rules(f, FRI), reply)

    def test_never_investment_advice_and_never_sending_as_him(self):
        f = self.facts()
        a, b, c = f[0], f[1], f[2]
        d = self.s.current(FRI, [], wait=True, ask=self.reply([
            self.item(a, title="Buy more NVDA shares"),
            self.item(b, title="Send the email to Dr Example"),
            self.item(c, title="Draft a reply to Dr Example")]))
        titles = [i["title"] for i in d["items"]]
        self.assertFalse(any("NVDA" in t for t in titles))
        self.assertIn(SHIPPED["suggest"]["draft_template"].format(title="Send the email to Dr Example"), titles)
        self.assertIn("Draft a reply to Dr Example", titles)

    def test_a_fresh_model_answer_is_kept_and_a_changed_fact_asks_again(self):
        f = self.facts()
        ask = mock.Mock(return_value=json.dumps({"items": [self.item(f[0])]}))
        self.s.current(FRI, [], ask=ask, wait=True)
        self.s.current(FRI, [], ask=ask, wait=True)
        self.assertEqual(ask.call_count, 1)
        w(self.vault, "hot.md", HOT.replace("Renew the library books", "Renew the library books today"))
        ask.return_value = json.dumps({"items": [self.item(self.facts()[0])]})
        self.s.current(FRI, [], ask=ask, wait=True)
        self.assertEqual(ask.call_count, 2)


class SaturdayTest(Base):
    def test_no_study_on_his_day_off_only_urgent_items(self):
        d = self.s.current(SAT, [], model_up=False)
        self.assertTrue(d["day_off"])
        self.assertEqual([i["title"] for i in d["items"]], ["Hackathon form: apply by 15 Nov"])
        self.assertFalse(any(i["kind"] == "study" for i in d["items"]))

    def test_the_model_cannot_slip_study_in_on_a_day_off(self):
        f = self.facts(SAT)
        study = self.by_title(f, "Study: Mod A")
        reply = json.dumps({"items": [{"title": "Revise", "why": "x", "minutes": 30, "source": study["id"], "kind": "admin"}]})
        d = self.s.current(SAT, [], ask=lambda t, c: reply, wait=True)
        self.assertEqual(d["mode"], "model")
        self.assertNotIn("Revise", [i["title"] for i in d["items"]])

    def test_nothing_urgent_says_day_off(self):
        w(self.vault, "hot.md", HOT.replace("apply by 15 Nov", "apply soon"))
        d = self.s.current(SAT, [], model_up=False)
        self.assertEqual(d["items"], [])
        self.assertIn(SHIPPED["suggest"]["words"]["day_off"], self.s.card(d))

    def test_the_day_off_is_config(self):
        self.cfg["suggest"]["days_off"] = ["Friday"]
        self._write_cfg()
        self.assertTrue(self.s.current(FRI, [], model_up=False)["day_off"])
        self.assertFalse(self.s.current(SAT, [], model_up=False)["day_off"])


class StateTest(Base):
    def state(self):
        return json.loads((self.review / SHIPPED["suggest"]["state_file"]).read_text(encoding="utf-8"))

    def test_done_persists_for_the_day(self):
        # an item not from hot.md: a hot.md one is struck, so it would not come back anyway
        first = next(i for i in self.s.current(FRI, [], model_up=False)["items"] if not i["line"])
        self.s.act("done", first["id"], FRI)
        self.assertIn(first["id"], self.state()["done"])
        later = FRI + dt.timedelta(hours=6)
        importlib.reload(self.s)  # a restarted server reads the file
        self.assertNotIn(first["id"], [i["id"] for i in self.s.current(later, [], model_up=False)["items"]])
        tomorrow = FRI + dt.timedelta(days=1, hours=1)
        self.cfg["suggest"]["days_off"] = []
        self._write_cfg()
        self.assertIn(first["id"], [i["id"] for i in self.s.current(tomorrow, [], model_up=False)["items"]])

    def test_snooze_hides_for_two_hours(self):
        first = self.s.current(FRI, [], model_up=False)["items"][0]
        self.s.act("snooze", first["id"], FRI)
        ids = lambda t: [i["id"] for i in self.s.current(t, [], model_up=False)["items"]]
        self.assertNotIn(first["id"], ids(FRI + dt.timedelta(minutes=SHIPPED["suggest"]["snooze_minutes"] - 1)))
        self.assertIn(first["id"], ids(FRI + dt.timedelta(minutes=SHIPPED["suggest"]["snooze_minutes"] + 1)))

    def test_bad_action_is_refused(self):
        with self.assertRaises(ValueError):
            self.s.act("delete", "x", FRI)
        with self.assertRaises(ValueError):
            self.s.act("done", "", FRI)

    def test_building_the_card_writes_nothing(self):
        before, vb = tree(self.review), tree(self.vault)
        self.s.card(self.s.current(FRI, [], model_up=False))
        self.assertEqual((tree(self.review), tree(self.vault)), (before, vb))


class StrikeTest(Base):
    def items(self, now=FRI):
        return {f["title"]: f for f in self.s.current(now, [], model_up=False)["facts"]}

    def test_done_strikes_the_exact_hot_line_and_keeps_crlf(self):
        w(self.vault, "hot.md", HOT, newline="\r\n")
        f = self.items()["Renew the library books"]
        out = self.s.act("done", f["id"], FRI)
        self.assertTrue(out["struck"])
        raw = self.hot.read_bytes()
        want = SHIPPED["suggest"]["strike"]["template"].format(
            lead="- ", text="Renew the library books", date=SHIPPED["suggest"]["strike"]["date"].format(d=13, mon="Nov"))
        self.assertIn(want.encode() + b"\r\n", raw)
        self.assertEqual(raw.count(b"\r\n"), HOT.count("\n"))  # nothing else changed
        self.assertNotIn("Renew the library books", self.items())  # struck lines are not facts

    def test_no_strike_when_the_line_changed(self):
        f = self.items()["Renew the library books"]
        w(self.vault, "hot.md", HOT.replace("Renew the library books", "Renew the library books (2)"))
        before = self.hot.read_bytes()
        out = self.s.act("done", f["id"], FRI)
        self.assertFalse(out["struck"])
        self.assertEqual(self.hot.read_bytes(), before)
        self.assertIn(f["id"], json.loads((self.review / SHIPPED["suggest"]["state_file"]).read_text())["done"])

    def test_no_strike_for_an_item_not_from_hot(self):
        f = self.items()["Study: Mod A"]
        before = tree(self.vault)
        self.assertFalse(self.s.act("done", f["id"], FRI)["struck"])
        self.assertEqual(tree(self.vault), before)


class CardTest(Base):
    def test_card_has_three_items_buttons_link_and_why_these(self):
        d = self.s.current(FRI, [], model_up=False)
        html = self.s.card(d)
        words = SHIPPED["suggest"]["words"]
        self.assertEqual(html.count('data-suggest="done"'), 3)
        self.assertEqual(html.count('data-suggest="snooze"'), 3)
        self.assertIn(words["why_toggle"], html)
        self.assertIn("obsidian://open?vault=vault&amp;file=hot", html)
        self.assertIn(words["minutes"].format(n=SHIPPED["suggest"]["minutes"]["by_kind"]["admin"]), html)
        for f in d["facts"]:
            self.assertIn(f["label"].replace("&", "&amp;"), html)

    def test_cli_dry_run_prints_three(self):
        out = io.StringIO()
        from atrium import board, __main__ as cli
        class Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return FRI
        clock = types.SimpleNamespace(datetime=Clock, date=dt.date, time=dt.time, timedelta=dt.timedelta)
        with mock.patch.object(board, "_ollama", return_value=None), contextlib.redirect_stdout(out), \
                mock.patch.object(out, "reconfigure", create=True), mock.patch.object(self.s, "dt", clock):
            self.assertEqual(cli.main(["suggest", "--dry-run"]), 0)
        lines = [l for l in out.getvalue().splitlines() if l[:2] in ("1.", "2.", "3.")]
        self.assertEqual(len(lines), 3)
        self.assertEqual(tree(self.review), {})


class HttpTest(Base):
    def setUp(self):
        super().setUp()
        from atrium import live
        self.cfg["live"].update(port=0)
        self._write_cfg()
        self.live = importlib.reload(live)
        srv = self.live.make_server()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.srv = srv
        self.fid = self.s.current(dt.datetime.now(), [], model_up=False)["facts"][0]["id"]

    def post(self, path, body, headers=None):
        c = http.client.HTTPConnection(*self.srv.server_address[:2], timeout=10)
        h = {"Content-Type": "application/json"} if headers is None else headers
        c.request("POST", path, body=json.dumps(body), headers=h)
        r = c.getresponse()
        out = r.status, r.read()
        c.close()
        return out

    def test_done_by_post(self):
        code, body = self.post(SHIPPED["suggest"]["http_path"], {"action": "done", "id": self.fid})
        self.assertEqual(code, 200, body)
        self.assertIn(self.fid, json.loads((self.review / SHIPPED["suggest"]["state_file"]).read_text())["done"])

    def test_other_posts_and_foreign_pages_are_refused(self):
        p = SHIPPED["suggest"]["http_path"]
        self.assertEqual(self.post("/", {})[0], 405)
        self.assertEqual(self.post("/data", {})[0], 405)
        self.assertEqual(self.post(p, {"action": "done", "id": self.fid}, headers={"Content-Type": "text/plain"})[0], 403)
        self.assertEqual(self.post(p, {"action": "done", "id": self.fid},
                                   headers={"Content-Type": "application/json", "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.post(p, {"action": "explode", "id": self.fid})[0], 400)
        self.assertFalse((self.review / SHIPPED["suggest"]["state_file"]).exists())


class ConfigTest(unittest.TestCase):
    def test_defaults_carry_the_same_keys(self):
        d = json.loads((Path(conf.DEFAULT).with_name("config_defaults.json")).read_text(encoding="utf-8"))["suggest"]
        self.assertEqual(set(d), set(SHIPPED["suggest"]))


if __name__ == "__main__":
    unittest.main()
