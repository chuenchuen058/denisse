"""python -m unittest atrium.test_live -v

The live Atrium page (atrium/live.py): collectors read a temp .review and a temp vault,
the server binds loopback only, the event stream sends an update when a file changes,
and nothing is ever written to .review. No network beyond loopback, no Ollama."""
import copy
import datetime as dt
import http.client
import importlib
import json
import os
import socket
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from atrium import conf

SHIPPED = json.loads(conf.DEFAULT.read_text(encoding="utf-8"))


def w(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def task(title, lane="claude", queue="auto", priority="normal"):
    return f"# {title}\nQueue: {queue}\nPriority: {priority}\nLane: {lane}\n\nDo the thing.\n"


def fake_review(review: Path, vault: Path, now: dt.datetime) -> None:
    """A small but complete Orchestra state: two runs today, one running, a queue,
    a draft, a parked task; a vault with a waiting list and study week notes."""
    day = now.date().isoformat()
    at = lambda h, m=0: f"{day}T{h:02d}:{m:02d}:00"
    hist = lambda end, secs, result, lane: {"end": end, "seconds": secs, "result": result, "lane": lane,
                                            "spent": "$1.50"}
    state = {"tasks": {
        "t-pass": {"history": [hist(at(1, 30), 1800, "PASS", "claude")], "last_result": "PASS"},
        "t-partial": {"history": [hist(at(2, 0), 2400, "PARTIAL", "copilot")], "last_result": "PARTIAL"},
        "t-run": {"history": [], "last_start": (now - dt.timedelta(minutes=20)).isoformat(timespec="seconds")}},
        "days": {day: {"cost_usd": 14.2, "work_s": 4200}},
        "lanes": {"codex": {"limited_until": (now + dt.timedelta(hours=3)).isoformat(timespec="seconds")}}}
    w(review, "state.json", json.dumps(state))
    w(review, "campaign.json", json.dumps({"enabled": True, "overrides": {"daily_spend_cap_usd": 60,
                                                                         "lane_slots": {"claude": 1, "codex": 1}}}))
    w(review, f"archive/{day}/t-pass.md", task("Ship the first thing"))
    w(review, "queue/t-run.md", task("Build the live page"))
    w(review, "queue/t-next.md", task("Next Claude job"))
    w(review, "queue/t-codex.md", task("Codex job", lane="codex"))
    w(review, "queue/t-draft.md", task("Draft idea", queue="draft"))
    w(review, "parked/t-parked.md", task("Stuck job"))
    w(review, "parked/t-parked.why.txt", "PARKED gate failed twice\n")
    w(review, "runner.claude.lock", json.dumps({"task": "t-run", "since": state["tasks"]["t-run"]["last_start"]}))
    w(vault, "hot.md", "## Waiting on Chuen\n- Run keats login once\n")
    mod = "Atlas/Academic/SEM 1 Modules/Mod A"
    w(vault, f"{mod}/Week 1/Mod A Week 1.md", "x" * 2000)
    w(vault, f"{mod}/Week 2/Mod A Week 2.md", "stub")
    (vault / mod / "Week 3").mkdir(parents=True)


def frozen_dt(now: dt.datetime):
    """A stand-in for the `datetime` module whose clock reads `now` (naive, local), so what the
    page shows cannot depend on the hour the tests run at (a run at 23:59 once failed)."""
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    class Day(dt.date):
        @classmethod
        def today(cls):
            return now.date()

    return types.SimpleNamespace(**dict(vars(dt), datetime=Clock, date=Day))


def tree(root: Path) -> dict:
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(root.rglob("*"))}


class LiveBase(unittest.TestCase):
    """A temp .review and vault, a temp atrium config, fresh modules; no data written yet."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.review, self.vault = self.tmp / "review", self.tmp / "vault"
        self.review.mkdir(), self.vault.mkdir()
        cfg = copy.deepcopy(SHIPPED)
        cfg["live"].update(port=0, poll_s=0.05, keepalive_s=0.5, refresh_s=3600)
        (self.tmp / "atrium.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.patches = [mock.patch.dict(os.environ, {"DENISSE_VAULT": str(self.vault),
                                                     "ATRIUM_CONFIG": str(self.tmp / "atrium.json"),
                                                     "ORCHESTRA_POWER": "battery:64"}),
                        mock.patch("urllib.request.urlopen", side_effect=OSError("no Ollama in tests"))]
        self.addCleanup(self._reload)  # runs last: after the servers close and the patches stop
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)  # cleanups run last-in first-out: after the servers are closed
        from atrium import context, snapshot, live
        ctx = importlib.reload(context)
        self.snap = importlib.reload(snapshot)
        self.snap.REVIEW = self.review
        self.live = importlib.reload(live)
        # a fixed clock, noon on a fixed day: nothing here may depend on when the tests run
        self.now = dt.datetime(2026, 6, 15, 12, 0, 0)
        clock = frozen_dt(self.now)
        for m in (ctx, self.snap, self.live):
            p = mock.patch.object(m, "dt", clock)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _reload():
        from atrium import context, snapshot
        importlib.reload(context), importlib.reload(snapshot)

    def serve(self):
        srv = self.live.make_server()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv

    def get(self, srv, path, method="GET"):
        c = http.client.HTTPConnection(*srv.server_address[:2], timeout=10)
        c.request(method, path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, r.getheader("Content-Type"), body


class LiveTest(LiveBase):
    def setUp(self):
        super().setUp()
        fake_review(self.review, self.vault, self.now)

    # -- collectors -------------------------------------------------------------
    def test_collectors_read_the_temp_review_and_vault(self):
        d = self.live.collect()
        ag = {a["lane"]: a for a in d["agents"]}
        self.assertEqual([a["lane"] for a in d["agents"]], [a["lane"] for a in SHIPPED["live"]["agents"]])
        self.assertEqual(ag["claude"]["fill"], 100)
        self.assertEqual(ag["claude"]["status"], SHIPPED["live"]["words"]["working"])
        self.assertEqual(ag["claude"]["queued"], SHIPPED["live"]["words"]["queued"].format(n=1))  # not the running one
        self.assertEqual(ag["claude"]["why"], SHIPPED["live"]["words"]["why_full"])
        self.assertEqual(ag["codex"]["state"], "bad")  # limited until later today
        self.assertEqual(ag["codex"]["held"], SHIPPED["live"]["words"]["held"])
        self.assertTrue(ag["codex"]["until"])
        self.assertTrue(ag["codex"]["status"].startswith(SHIPPED["live"]["words"]["until"].split("{")[0]))
        self.assertEqual(ag["copilot"]["fill"], 0)
        # needs you: the vault's list first, then drafts and parked, at most needs_max
        self.assertEqual(len(d["needs"]), SHIPPED["live"]["needs_max"])
        self.assertEqual([n["kind"] for n in d["needs"]], ["note", "draft", "parked"])
        self.assertIn("Run keats login once", d["needs"][0]["item"])
        self.assertIn("gate failed twice", d["needs"][2]["item"])
        # today's timeline: two finished runs and the one still going
        tl = {b["lane"] + ":" + b["cls"]: b for b in d["timeline"]}
        self.assertEqual(set(tl), {"claude:ok", "copilot:warn", "claude:run"})
        self.assertEqual((tl["claude:ok"]["start"], tl["claude:ok"]["end"]), (1.0, 1.5))
        self.assertEqual(tl["claude:ok"]["task"], "Ship the first thing")
        self.assertIsNone(tl["claude:run"]["end"])
        self.assertEqual(tl["claude:run"]["task"], "Build the live page")
        # progress: 1 done of 5 touched today (2 ran, 1 running, 2 more queued), 1 of 3 weeks, $14 of $60
        pr = {p["key"]: p for p in d["progress"]}
        self.assertEqual(pr["tasks"]["text"], "1 of 5 done")
        self.assertEqual(pr["study"]["text"], "1 of 3 weeks")
        self.assertEqual(pr["spend"]["text"], "$14 of $60")
        self.assertAlmostEqual(pr["spend"]["pct"], round(100 * 14.2 / 60, 1))
        self.assertEqual(d["power"], {"ac": False, "battery_percent": 64})

    def test_panel_words_and_agents_come_from_config(self):
        cfg = json.loads((self.tmp / "atrium.json").read_text(encoding="utf-8"))
        cfg["live"]["agents"] = [{"lane": "codex", "label": "Cx", "colour": "#123456"}]
        cfg["live"]["needs_max"] = 1
        cfg["live"]["words"]["held"] = "on hold"
        (self.tmp / "atrium.json").write_text(json.dumps(cfg), encoding="utf-8")
        d = self.live.collect()
        self.assertEqual([(a["label"], a["colour"], a["held"]) for a in d["agents"]], [("Cx", "#123456", "on hold")])
        self.assertEqual(len(d["needs"]), 1)

    def test_snapshot_hub_tables_unchanged(self):
        t = self.snap.tables(["tasks", "waiting"])
        # no live-only keys; until = a lane-limit note's reset, for the hub page to drop it live
        self.assertTrue(all(set(r) == {"item", "from", "until", "n"} for r in t["waiting"]))
        out = self.tmp / "hub"
        with mock.patch.object(self.snap, "_agents", return_value=[]), \
                mock.patch.object(self.snap, "_learner", return_value=[]), \
                mock.patch.object(self.snap, "_study", return_value=[]):
            self.snap.write(out)
        self.assertEqual(sorted(p.stem for p in out.glob("*.json")), sorted(self.snap.TABLES))

    # -- server -----------------------------------------------------------------
    def test_binds_loopback_only(self):
        srv = self.serve()
        self.assertEqual(srv.server_address[0], "127.0.0.1")
        for host in ("0.0.0.0", "", "192.168.1.5"):
            with self.assertRaises(ValueError):
                self.live.make_server(host=host or "0.0.0.0")

    def test_busy_port_is_rejected(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            with self.assertRaises(OSError):
                self.live.make_server(port=port)

    def test_serves_page_and_data_and_refuses_writes(self):
        srv = self.serve()
        code, ctype, body = self.get(srv, "/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"app.js", body)
        self.assertNotIn(b"cdn", body.lower())
        for f in ("app.js", "style.css"):
            self.assertEqual(self.get(srv, "/" + f)[0], 200)
        code, ctype, body = self.get(srv, "/data")
        d = json.loads(body)
        self.assertIn("now_h", d)
        self.assertEqual(d["version"], 1)
        self.assertEqual(self.get(srv, "/../config.json")[0], 404)
        self.assertEqual(self.get(srv, "/live.py")[0], 404)
        self.assertEqual(self.get(srv, "/data", "POST")[0], 405)

    def test_event_stream_sends_an_update_when_a_file_changes(self):
        srv = self.serve()
        c = http.client.HTTPConnection(*srv.server_address[:2], timeout=10)
        c.request("GET", "/events")
        r = c.getresponse()
        self.assertIn("text/event-stream", r.getheader("Content-Type"))

        def next_event():
            while True:
                line = r.fp.readline().decode("utf-8")
                self.assertTrue(line, "stream closed")
                if line.startswith("data: "):
                    return json.loads(line[6:])

        first = next_event()
        claude = next(a for a in first["agents"] if a["lane"] == "claude")
        self.assertEqual(claude["queued"], SHIPPED["live"]["words"]["queued"].format(n=1))
        w(self.review, "queue/t-more.md", task("One more"))
        second = next_event()
        self.assertGreater(second["version"], first["version"])
        claude = next(a for a in second["agents"] if a["lane"] == "claude")
        self.assertEqual(claude["queued"], SHIPPED["live"]["words"]["queued"].format(n=2))
        c.close()

    def test_no_work_without_a_tab(self):
        with mock.patch.object(self.live, "collect", wraps=self.live.collect) as col:
            srv = self.serve()
            time.sleep(0.3)  # several poll_s with nobody connected
            self.assertEqual(col.call_count, 0)
            self.get(srv, "/data")
            self.assertEqual(col.call_count, 1)

    def test_a_closed_tab_stops_the_polling(self):
        with mock.patch.object(self.live, "fingerprint", wraps=self.live.fingerprint) as fp:
            srv = self.serve()
            c = http.client.HTTPConnection(*srv.server_address[:2], timeout=10)
            c.request("GET", "/events")
            r = c.getresponse()
            while not r.fp.readline().startswith(b"data: "):
                pass
            time.sleep(0.2)
            self.assertGreater(fp.call_count, 1)  # polling while the tab is open
            r.close()
            c.close()
            time.sleep(0.3)
            n = fp.call_count
            time.sleep(0.3)
            self.assertEqual(fp.call_count, n)

    def test_nothing_writes_to_review(self):
        before = tree(self.review)
        srv = self.serve()
        self.live.collect()
        for p in ("/", "/data", "/app.js"):
            self.get(srv, p)
        c = http.client.HTTPConnection(*srv.server_address[:2], timeout=10)
        c.request("GET", "/events")
        r = c.getresponse()
        while not r.fp.readline().startswith(b"data: "):
            pass
        c.close()
        self.assertEqual(tree(self.review), before)

    # -- installer --------------------------------------------------------------
    def test_installer_builds_startup_and_open_shortcuts_from_config(self):
        from atrium import install_live
        importlib.reload(install_live)
        s = install_live.script(Path("C:/repo"))
        self.assertIn("'Startup'", s)
        self.assertIn("live_task.py", s)
        self.assertIn("pythonw.exe", s)
        self.assertIn(self.live.url(), s)
        for sc in SHIPPED["install_live"]["shortcuts"]:
            self.assertIn(sc["name"], s)


class LiveScenarioTest(LiveBase):
    """Each collector against one .review shape at a time: empty (and absent), running,
    parked, limited (either source), on battery. Starts from an empty .review and vault."""

    def setUp(self):
        super().setUp()
        self.words = SHIPPED["live"]["words"]

    def agents(self):
        return {a["lane"]: a for a in self.live.collect()["agents"]}

    def set_power(self, value):
        p = mock.patch.dict(os.environ, {"ORCHESTRA_POWER": value})
        p.start()
        self.addCleanup(p.stop)

    def assert_empty_page(self, d):
        self.assertEqual([a["lane"] for a in d["agents"]], [a["lane"] for a in SHIPPED["live"]["agents"]])
        ollama = {u["lane"] for u in SHIPPED["snapshot"]["usage"] if u.get("source") == "ollama"}
        for a in d["agents"]:
            self.assertEqual((a["fill"], a["queued"], a["why"], a["held"], a["until"]), (0, "", "", "", ""), a["lane"])
            # Ollama is refused in tests, so its lane is honestly "bad" (down); every other lane is free
            self.assertEqual(a["state"], "bad" if a["lane"] in ollama else "ok", a["lane"])
        self.assertEqual(d["needs"], [])
        self.assertEqual(d["timeline"], [])
        self.assertEqual(d["where"]["overlaps"], [])
        pr = {p["key"]: p for p in d["progress"]}
        self.assertEqual([pr[k]["pct"] for k in ("tasks", "study")], [0, 0])
        self.assertEqual(pr["tasks"]["text"], "0 of 0 done")
        self.assertEqual(pr["study"]["text"], "0 of 0 weeks")
        json.dumps(d)  # still JSON-able

    # -- empty --------------------------------------------------------------------
    def test_empty_review(self):
        self.assert_empty_page(self.live.collect())
        self.assertEqual(self.live.fingerprint(), ())

    def test_study_count_uses_week_note_status_when_present(self):
        mod = "Atlas/Academic/SEM 1 Modules/Mod A"
        w(self.vault, f"{mod}/Week 1/Mod A Week 1.md", "---\nstatus: done\n---\n" + ("x" * 2000))
        w(self.vault, f"{mod}/Week 2/Mod A Week 2.md", "---\nstatus: blocked\n---\n" + ("x" * 2000))
        w(self.vault, f"{mod}/Week 3/Mod A Week 3.md", "---\nstatus: unknown\n---\n" + ("x" * 2000))
        d = self.live.collect()
        self.assertEqual({p["key"]: p["text"] for p in d["progress"]}["study"], "1 of 3 weeks")

    def test_review_folder_absent(self):
        import shutil
        shutil.rmtree(self.review)
        self.assert_empty_page(self.live.collect())
        self.assertEqual(self.live.fingerprint(), ())
        self.assertFalse(self.review.exists())  # looking did not create it

    def test_empty_review_is_served(self):
        srv = self.serve()
        code, ctype, body = self.get(srv, "/data")
        self.assertEqual(code, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual(json.loads(body)["needs"], [])

    def test_unreadable_state_json_is_an_empty_page(self):
        w(self.review, "state.json", "{not json")
        w(self.review, "runner.claude.lock", "{half a lock")
        self.assert_empty_page(self.live.collect())

    # -- running ------------------------------------------------------------------
    def test_running_task_without_a_queue_file_shows_its_id(self):
        since = (self.now - dt.timedelta(minutes=7)).isoformat(timespec="seconds")
        w(self.review, "runner.claude.lock", json.dumps({"task": "t-orphan", "since": since}))
        d = self.live.collect()
        ag = {a["lane"]: a for a in d["agents"]}
        self.assertEqual((ag["claude"]["state"], ag["claude"]["status"]), ("busy", self.words["working"]))
        self.assertEqual(ag["claude"]["queued"], "")
        run = [b for b in d["timeline"] if b["cls"] == "run"]
        self.assertEqual(len(run), 1)
        self.assertEqual(run[0]["task"], "t-orphan")
        self.assertIsNone(run[0]["end"])
        # no last_start in state.json: the block starts at the lock's age
        midnight = dt.datetime.combine(self.now.date(), dt.time())
        expect = max(0.0, (self.now - dt.timedelta(minutes=7) - midnight).total_seconds() / 3600)
        self.assertAlmostEqual(run[0]["start"], expect, delta=0.05)
        self.assertEqual({p["key"]: p["text"] for p in d["progress"]}["tasks"], "0 of 1 done")

    def test_idle_lock_is_not_a_running_task(self):
        w(self.review, "runner.claude.lock", json.dumps({"task": None}))
        self.assertEqual(self.agents()["claude"]["state"], "ok")

    def test_yesterdays_runs_are_not_on_todays_timeline(self):
        y = (self.now - dt.timedelta(days=1)).replace(hour=12).isoformat(timespec="seconds")
        w(self.review, "state.json", json.dumps({"tasks": {"t-old": {"history": [
            {"end": y, "seconds": 60, "result": "PASS", "lane": "claude"}]}}}))
        d = self.live.collect()
        self.assertEqual(d["timeline"], [])
        self.assertEqual({p["key"]: p["text"] for p in d["progress"]}["tasks"], "0 of 0 done")

    def test_a_run_that_started_yesterday_is_clipped_to_midnight(self):
        end = dt.datetime.combine(self.now.date(), dt.time(0, 30))
        w(self.review, "state.json", json.dumps({"tasks": {"t-late": {"history": [
            {"end": end.isoformat(), "seconds": 7200, "result": "FAIL", "lane": "codex"}]}}}))
        (b,) = self.live.collect()["timeline"]
        self.assertEqual((b["start"], b["end"], b["cls"]), (0.0, 0.5, SHIPPED["live"]["timeline"]["default_class"]))

    # -- parked -------------------------------------------------------------------
    def test_parked_task_needs_you_and_is_not_queued(self):
        w(self.review, "parked/t-p.md", task("Stuck job"))
        w(self.review, "parked/t-p.why.txt", "PARKED gate failed twice\n")
        w(self.review, "parked/t-p.report.md", "RESULT: FAIL\n")
        d = self.live.collect()
        self.assertEqual([n["kind"] for n in d["needs"]], ["parked"])
        self.assertIn("Stuck job", d["needs"][0]["item"])
        self.assertIn("gate failed twice", d["needs"][0]["item"])
        self.assertEqual(d["needs"][0]["icon"], SHIPPED["live"]["needs_icons"]["parked"])
        self.assertEqual({a["lane"]: a for a in d["agents"]}["claude"]["queued"], "")

    def test_parked_without_a_reason_uses_the_default(self):
        snap_cfg = SHIPPED["snapshot"]["waiting"]
        w(self.review, "parked/t-p.md", task("Stuck job"))
        (n,) = self.live.collect()["needs"]
        self.assertIn(snap_cfg["parked_default"][:20], n["item"])

    def test_drafts_and_parked_need_you_in_that_order_capped(self):
        w(self.vault, "hot.md", "## Waiting on Chuen\n- One\n- Two\n")
        w(self.review, "queue/t-d.md", task("Draft idea", queue="draft"))
        w(self.review, "parked/t-p.md", task("Stuck job"))
        d = self.live.collect()
        self.assertEqual([n["kind"] for n in d["needs"]], ["note", "note", "draft"][:SHIPPED["live"]["needs_max"]])

    # -- limited ------------------------------------------------------------------
    def test_lane_limited_by_the_runners_record(self):
        until = (self.now + dt.timedelta(hours=2)).replace(second=0, microsecond=0)
        w(self.review, "state.json", json.dumps({"tasks": {}, "lanes": {"codex": {
            "limited_until": until.isoformat()}}}))
        w(self.review, "queue/t-c.md", task("Codex job", lane="codex"))
        a = self.agents()["codex"]
        self.assertEqual(a["state"], "bad")
        self.assertEqual(a["status"], self.words["until"].format(time=until.strftime(self.words["until_format"])))
        self.assertEqual(a["until"], until.isoformat(timespec="minutes"))
        self.assertEqual((a["queued"], a["held"], a["why"]),
                         (self.words["queued"].format(n=1), self.words["held"], self.words["why_limited"]))

    def test_a_lifted_limit_is_not_shown(self):
        w(self.review, "state.json", json.dumps({"tasks": {}, "lanes": {"codex": {
            "limited_until": (self.now - dt.timedelta(minutes=5)).isoformat()}}}))
        a = self.agents()["codex"]
        self.assertEqual((a["state"], a["status"], a["until"], a["held"]), ("ok", self.words["free"], "", ""))

    def test_a_garbled_limit_is_ignored(self):
        w(self.review, "state.json", json.dumps({"tasks": {}, "lanes": {"codex": {"limited_until": "soon"},
                                                                       "claude": None}}))
        self.assertEqual(self.agents()["codex"]["state"], "ok")

    def test_lane_limited_by_the_runner_log(self):
        until = (self.now + dt.timedelta(hours=2)).strftime("%H:%M")  # the fixed noon clock: still today
        w(self.review, "runner.log", f"{self.now.isoformat(timespec='seconds')} LIMITED t-x lane=codex until {until}\n")
        w(self.review, "queue/t-c.md", task("Codex job", lane="codex"))
        a = self.agents()["codex"]
        self.assertEqual(a["state"], "bad")
        self.assertIn(until, a["status"])  # the usage row's own status
        self.assertEqual((a["held"], a["why"]), (self.words["held"], self.words["why_limited"]))

    # -- on battery ---------------------------------------------------------------
    def test_on_battery_queued_work_says_why(self):
        self.set_power("battery:30")
        w(self.review, "queue/t-c.md", task("Claude job"))
        d = self.live.collect()
        self.assertEqual(d["power"], {"ac": False, "battery_percent": 30})
        a = {x["lane"]: x for x in d["agents"]}["claude"]
        self.assertEqual((a["why"], a["state"], a["held"]), (self.words["why_battery"], "ok", ""))

    def test_on_ac_queued_work_gives_no_reason(self):
        self.set_power("ac")
        w(self.review, "queue/t-c.md", task("Claude job"))
        d = self.live.collect()
        self.assertIs(d["power"]["ac"], True)
        self.assertEqual({x["lane"]: x for x in d["agents"]}["claude"]["why"], self.words["why_none"])

    def test_power_unknown_is_shown_as_nothing(self):
        self.set_power("unknown")
        self.assertEqual(self.live.collect()["power"], {"ac": None, "battery_percent": None})
        with mock.patch("orchestra.power.power_status", side_effect=RuntimeError("no ctypes")):
            self.assertEqual(self.live.collect()["power"], {"ac": None, "battery_percent": None})

    # -- the stream and the watch list ----------------------------------------------
    def test_fingerprint_sees_watched_files_and_not_others(self):
        before = self.live.fingerprint()
        w(self.review, "reports/t.lane.log", "noise")  # not watched
        self.assertEqual(self.live.fingerprint(), before)
        for rel in ("state.json", "queue/t.md", f"archive/{self.now.date()}/t.md", "runner.claude.lock"):
            w(self.review, rel, "x")
            after = self.live.fingerprint()
            self.assertNotEqual(after, before, rel)
            before = after
        w(self.vault, "hot.md", "## Waiting on Chuen\n- x\n")
        self.assertNotEqual(self.live.fingerprint(), before)

    def test_event_stream_pushes_a_vault_change(self):
        srv = self.serve()
        c = http.client.HTTPConnection(*srv.server_address[:2], timeout=10)
        c.request("GET", "/events")
        r = c.getresponse()

        def next_event():
            while True:
                line = r.fp.readline().decode("utf-8")
                self.assertTrue(line, "stream closed")
                if line.startswith("data: "):
                    return json.loads(line[6:])

        self.assertEqual(next_event()["needs"], [])
        w(self.vault, "hot.md", "## Waiting on Chuen\n- Sign the form\n")
        second = next_event()
        self.assertEqual([n["kind"] for n in second["needs"]], ["note"])
        c.close()

    def test_event_stream_sends_nothing_new_when_nothing_changed(self):
        srv = self.serve()
        with mock.patch.object(self.live, "collect", wraps=self.live.collect) as col:
            c = http.client.HTTPConnection(*srv.server_address[:2], timeout=10)
            c.request("GET", "/events")
            r = c.getresponse()
            lines = []
            deadline = time.monotonic() + 1.2  # past keepalive_s (0.5)
            while time.monotonic() < deadline:
                line = r.fp.readline()
                if not line:
                    break
                lines.append(line)
            c.close()
        self.assertEqual(sum(l.startswith(b"data: ") for l in lines), 1)
        self.assertIn(b": keepalive\n", lines)
        self.assertEqual(col.call_count, 1)  # refresh_s is an hour: stat only after the first

    # -- server -------------------------------------------------------------------
    def test_listening_socket_is_bound_to_loopback(self):
        # what the OS actually bound, not what was asked for: no wildcard, nothing on the LAN
        # (dialling the LAN address to prove it would itself be refused by the checks guard)
        import ipaddress
        srv = self.serve()
        host, port = srv.socket.getsockname()[:2]
        self.assertTrue(ipaddress.ip_address(host).is_loopback, host)
        self.assertEqual(host, SHIPPED["live"]["host"])
        self.assertGreater(port, 0)
        self.assertEqual(self.get(srv, "/data")[0], 200)

    def test_configured_host_off_loopback_is_refused(self):
        cfg = json.loads((self.tmp / "atrium.json").read_text(encoding="utf-8"))
        cfg["live"]["host"] = "0.0.0.0"
        (self.tmp / "atrium.json").write_text(json.dumps(cfg), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.live.make_server()

    def test_unknown_paths_404_and_writes_405(self):
        srv = self.serve()
        for p in ("/nope.html", "/live_page/index.html", "/..%2Fconfig.json", "/%2e%2e/live.py"):
            self.assertEqual(self.get(srv, p)[0], 404, p)
        for m in ("PUT", "DELETE", "PATCH"):
            self.assertEqual(self.get(srv, "/", m)[0], 405, m)
        self.assertEqual(self.get(srv, "/?x=1")[0], 200)

    def test_nothing_writes_to_review_or_vault_across_scenarios(self):
        fake_review(self.review, self.vault, self.now)
        w(self.review, "runner.log", f"{self.now.isoformat(timespec='seconds')} LIMITED t-x lane=codex until 23:59\n")
        before, vault_before = tree(self.review), tree(self.vault)
        srv = self.serve()
        for power in ("ac", "battery:10", "unknown"):
            self.set_power(power)
            self.live.collect()
            self.live.fingerprint()
            self.get(srv, "/data")
        self.assertEqual(tree(self.review), before)
        self.assertEqual(tree(self.vault), vault_before)


class InstallLiveTest(unittest.TestCase):
    """The installer prints its command and runs nothing unless --register; it uses the
    per-user Startup folder, not schtasks (atrium/config.json install_live _note)."""

    def setUp(self):
        from atrium import install_live
        self.inst = importlib.reload(install_live)
        self.cfg = SHIPPED["install_live"]

    def test_dry_run_prints_the_command_and_registers_nothing(self):
        import io
        out = io.StringIO()
        with mock.patch("subprocess.run") as run, mock.patch("sys.stdout", out):
            self.assertEqual(self.inst.main([]), 0)
        run.assert_not_called()
        printed = out.getvalue()
        self.assertTrue(printed.startswith(" ".join(self.cfg["powershell"][:2])))
        self.assertIn("live_task.py", printed)
        self.assertNotIn("schtasks", printed.lower())

    def test_register_runs_exactly_that_command(self):
        import io
        with mock.patch("subprocess.run") as run, mock.patch("sys.stdout", io.StringIO()):
            run.return_value.returncode = 0
            self.assertEqual(self.inst.main(["--register"]), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], self.inst.command())

    def test_command_shape(self):
        repo = Path("C:/Users/Some One/it's repo")
        cmd = self.inst.command(repo)
        self.assertEqual(cmd[:-1], self.cfg["powershell"])
        s = cmd[-1]
        self.assertEqual(s, self.inst.script(repo))
        # one shortcut runs the venv's pythonw on live_task.py, hidden (WindowStyle 7), from the repo
        self.assertEqual(s.count("CreateShortcut"), sum(bool(x.get("run")) for x in self.cfg["shortcuts"]))
        self.assertIn(self.inst._q(repo / self.cfg["python"]), s)
        self.assertIn(self.inst._q('"' + str(repo / self.cfg["script"]) + '"'), s)
        self.assertIn(f"$l.WorkingDirectory = {self.inst._q(repo)}", s)
        self.assertIn("$l.WindowStyle = 7", s)
        self.assertIn("it''s repo", s)  # quotes in the path survive PowerShell quoting
        for sc in self.cfg["shortcuts"]:
            self.assertIn(f"GetFolderPath({self.inst._q(sc['folder'])})", s)
        self.assertEqual(s.count("URL="), sum(not x.get("run") for x in self.cfg["shortcuts"]))

    def test_browser_override_is_used_in_script(self):
        browser = "C:/Programs/Opera GX/opera.exe"
        s = self.inst.script(browser=browser)
        self.assertIn(self.inst._q(browser), s)
        self.assertIn(self.inst.live.url(), s)

    def test_default_repo_is_this_checkout(self):
        here = Path(__file__).resolve().parents[1]
        self.assertIn(str(here / self.cfg["script"]), self.inst.script())


class LiveTaskTest(unittest.TestCase):
    """The logon entry point: a taken port is logged and not an error; a crash is logged."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        p = mock.patch.dict(os.environ, {"DENISSE_VAULT": str(self.tmp)})
        p.start()
        self.addCleanup(p.stop)
        from atrium import context, live_task
        importlib.reload(context)
        self.addCleanup(importlib.reload, context)
        self.lt = importlib.reload(live_task)
        self.log = self.tmp / SHIPPED["install_live"]["log_file"]

    def test_port_taken_is_logged_and_exit_0(self):
        with mock.patch.object(self.lt.live, "serve", side_effect=OSError("address in use")):
            self.assertEqual(self.lt.main(), 0)
        self.assertIn("address in use", self.log.read_text(encoding="utf-8"))

    def test_crash_is_logged_and_exit_1(self):
        with mock.patch.object(self.lt.live, "serve", side_effect=KeyError("boom")):
            self.assertEqual(self.lt.main(), 1)
        self.assertIn("KeyError", self.log.read_text(encoding="utf-8"))

    def test_clean_exit_writes_no_log(self):
        with mock.patch.object(self.lt.live, "serve", return_value=0):
            self.assertEqual(self.lt.main(), 0)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
