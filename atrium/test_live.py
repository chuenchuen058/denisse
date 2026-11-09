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
import tempfile
import threading
import time
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


def tree(root: Path) -> dict:
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(root.rglob("*"))}


class LiveTest(unittest.TestCase):
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
        importlib.reload(context)
        self.snap = importlib.reload(snapshot)
        self.snap.REVIEW = self.review
        self.live = importlib.reload(live)
        self.now = dt.datetime.now()
        fake_review(self.review, self.vault, self.now)

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
        self.assertTrue(all(set(r) == {"item", "from", "n"} for r in t["waiting"]))  # no live-only keys
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
