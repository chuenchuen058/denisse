"""python -m unittest atrium.test_gamemode_hub -v

The hub's Game mode switch (atrium/live.py, atrium/board.py; the work is orchestra/gamemode.py):
the button in both states and its line, the page rebuilding nothing while game mode is on, the
POST endpoint's guard, and the hourly digest skipping. orchestra.gamemode.on/off are replaced:
nothing is paused, killed or unloaded here."""
import http.client
import importlib
import json
from unittest import mock

from atrium.test_live import SHIPPED, LiveBase, fake_review

GM = SHIPPED["gamemode"]


class GameModeHubTest(LiveBase):
    def setUp(self):
        super().setUp()
        fake_review(self.review, self.vault, self.now)
        from atrium import board
        self.board = importlib.reload(board)

    def set_mode(self, **st):
        (self.review / "gamemode.json").write_text(json.dumps(st), encoding="utf-8")

    def post(self, srv, path, body, headers=None, raw=None):
        c = http.client.HTTPConnection(*srv.server_address[:2], timeout=10)
        h = {"Content-Type": "application/json", **(headers or {})}
        c.request("POST", path, raw if raw is not None else json.dumps(body), headers=h)
        r = c.getresponse()
        out = r.status, r.read()
        c.close()
        return out

    # -- the button and its line
    def test_button_renders_off_and_on(self):
        off = self.board.dress_header('<div></div><button class="theme" id="theme" type="button">T</button>',
                                      {"on": False}, 2)
        self.assertIn('id="gamemode"', off)
        self.assertLess(off.index('id="gamemode"'), off.index('id="theme"'))  # next to Theme
        self.assertIn(GM["label_off"], off)
        self.assertIn('data-running="2"', off)
        self.assertNotIn("gm-line", off)
        on = self.board.dress_header('<button class="theme" id="theme" type="button">T</button>',
                                     {"on": True, "since": "2026-06-15T11:14:00", "paused": 2, "unloaded": True}, 0)
        self.assertIn(GM["label_on"], on)
        self.assertIn('aria-pressed="true"', on)
        self.assertIn(GM["colour_on"], on)
        line = GM["line"].format(time="11:14", paused=GM["paused_many"].format(n=2), ollama=GM["ollama_unloaded"])
        self.assertIn(line, on.replace("&#x27;", "'"))

    def test_the_live_page_shows_the_switch_and_its_settings(self):
        v, d = self.live.Hub().get()
        self.assertIn('id="gamemode"', d["board"]["header"])
        self.assertEqual(d["gamemode"]["http_path"], GM["http_path"])
        self.assertEqual(d["gamemode"]["key"], GM["key"])
        self.assertFalse(d["gamemode"]["on"])
        self.assertIn('data-running="1"', d["board"]["header"])  # fake_review has one task running

    def test_nothing_is_rebuilt_while_game_mode_is_on(self):
        hub = self.live.Hub()
        with mock.patch.object(self.live, "collect", wraps=self.live.collect) as col, \
                mock.patch.object(self.live, "fingerprint", wraps=self.live.fingerprint) as fp:
            v1, d1 = hub.get()
            self.assertEqual(col.call_count, 1)
            self.set_mode(on=True, since="2026-06-15T11:14:00Z", paused=["t-run"], ollama={"unloaded": []})
            v2, d2 = hub.get()
            self.assertGreater(v2, v1)  # the switch and its line changed: a new version for the page
            self.assertIn("gm-line", d2["board"]["header"])
            self.assertTrue(d2["gamemode"]["on"])
            (self.review / "queue" / "t-more.md").write_text("# More\nQueue: auto\n", encoding="utf-8")
            n = fp.call_count
            v3, _ = hub.get()
            self.assertEqual((col.call_count, fp.call_count, v3), (1, n, v2))  # no build, not even a stat
            self.set_mode(on=False)
            v4, d4 = hub.get()
            self.assertEqual(col.call_count, 2)  # off: the page catches up
            self.assertNotIn("gm-line", d4["board"]["header"])

    # -- the endpoint
    def test_endpoint_switches_through_gamemode(self):
        srv = self.serve()
        calls = []
        with mock.patch.object(self.live, "switch_gamemode", lambda on: calls.append(on) or {"on": on, "lines": []}):
            code, body = self.post(srv, GM["http_path"], {"on": True})
            self.assertEqual((code, json.loads(body)["on"]), (200, True))
            code, _ = self.post(srv, GM["http_path"], {"on": False},
                                headers={"Origin": f"http://127.0.0.1:{srv.server_address[1]}"})
            self.assertEqual(code, 200)
            self.assertEqual(self.post(srv, GM["http_path"], {"on": "yes"})[0], 400)
        self.assertEqual(calls, [True, False])

    def test_endpoint_rejects_non_loopback_cross_origin_and_non_json(self):
        srv = self.serve()
        port = srv.server_address[1]
        with mock.patch.object(self.live, "switch_gamemode", side_effect=AssertionError("switched")):
            self.assertEqual(self.post(srv, GM["http_path"], {"on": True}, headers={"Host": f"evil.example:{port}"})[0], 403)
            self.assertEqual(self.post(srv, GM["http_path"], {"on": True},
                                       headers={"Origin": "http://evil.example"})[0], 403)
            self.assertEqual(self.post(srv, GM["http_path"], None, headers={"Content-Type": "text/plain"},
                                       raw='{"on": true}')[0], 403)
            self.assertEqual(self.post(srv, "/elsewhere", {"on": True})[0], 405)

    def test_the_hourly_digest_skips_while_on(self):
        from atrium import digest_task
        dt_mod = importlib.reload(digest_task)
        with mock.patch("orchestra.gamemode.is_on", return_value=True), \
                mock.patch.object(dt_mod.digest, "run", side_effect=AssertionError("digest ran")):
            self.assertEqual(dt_mod.main([]), 0)


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
