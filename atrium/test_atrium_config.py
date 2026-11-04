"""python -m unittest atrium.test_atrium_config -v

atrium/config.json drives atrium/ (AGENTS.md Rule 0): each test edits one value in a
temp copy (ATRIUM_CONFIG), reloads, and checks the behaviour follows the data.
Temp vault, temp .review, temp home; no network, no real vault."""
import copy
import datetime as dt
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from atrium import conf

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = json.loads(conf.DEFAULT.read_text(encoding="utf-8"))


def w(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class ConfigDrivesAtrium(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.v = self.tmp / "vault"
        self.v.mkdir()
        self.cfg_path = self.tmp / "atrium.json"
        self.env = mock.patch.dict(os.environ, {"DENISSE_VAULT": str(self.v), "ATRIUM_CONFIG": str(self.cfg_path)})
        self.env.start()
        self.cfg = copy.deepcopy(SHIPPED)
        self.today = dt.date.today().isoformat()

    def tearDown(self):
        self.env.stop()
        self.mods()  # back to the shipped data for whatever runs next

    def mods(self):
        """Write self.cfg (if the test is still running) and reload every atrium module."""
        if "ATRIUM_CONFIG" in os.environ:
            self.cfg_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        from atrium import context, handoff, credits, capture, snapshot
        ctx = importlib.reload(context)
        return (ctx, importlib.reload(handoff), importlib.reload(credits),
                importlib.reload(capture), importlib.reload(snapshot))

    # -- context ---------------------------------------------------------
    def test_context_parts_budget_and_header_come_from_data(self):
        w(self.v, "Atlas/Settled.md", "".join(f"- 2026-10-{i:02d} [Claude] fact {i}\n" for i in range(1, 21)))
        w(self.v, "hot.md", "## Active right now\n- shipping the hub\n")
        ctx = self.mods()[0]
        out = ctx.show("claude")
        self.assertIn("fact 9\n", out)        # shipped: the last 12 settled lines
        self.assertIn("shipping the hub", out)
        c = self.cfg["context"]
        next(p for p in c["parts"] if p["note"] == "Atlas/Settled.md")["last"] = 2
        c["parts"] = [p for p in c["parts"] if p.get("section") != "Active right now"]
        c["header"] = "# Pack for {lane}"
        c["lanes"].append("newlane")
        c["budget"]["newlane"] = 300
        ctx = self.mods()[0]
        out = ctx.show("claude")
        self.assertNotIn("fact 9\n", out)
        self.assertIn("fact 20", out)
        self.assertNotIn("shipping the hub", out)
        self.assertTrue(out.startswith("# Pack for claude\n"))
        self.assertIn("newlane", ctx.LANES)
        self.assertLess(len(ctx.show("newlane")), 300 + 100)

    def test_vault_fallback_is_data(self):
        os.environ.pop("DENISSE_VAULT")
        self.cfg["vault"]["fallback"] = "some/other vault"
        with mock.patch.dict(sys.modules, {"settings": None}):  # settings.vault() unavailable
            ctx = self.mods()[0]
        self.assertEqual(ctx.VAULT, Path.home() / "some/other vault")

    # -- handoff / credits -----------------------------------------------
    def test_handoff_line_and_note_shape_come_from_data(self):
        self.cfg["handoff"].update(note="Logs/{date}.md", line="* {agent} @ {time}: {text}", line_chars=5,
                                   new_note="# {date}\n")
        h = self.mods()[1]
        self.assertTrue(h.remember("codex", ["abcdefghij"]))
        text = (self.v / "Logs" / f"{self.today}.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(f"# {self.today}\n"))
        self.assertRegex(text, r"\* codex @ \d\d:\d\d: abcde\n")

    def test_credit_tags_and_notes_come_from_data(self):
        w(self.v, "Atlas/Settled.md", "- 2026-11-01 [Thomas] picked the hub\n")
        w(self.v, "Notes/Extra.md", "- 2026-11-02 untagged\n")
        cr = self.mods()[2]
        self.assertEqual([b[2] for b in cr.check()], ["- 2026-11-01 [Thomas] picked the hub"])
        self.cfg["credits"]["tags"].append("Thomas")
        self.cfg["credits"]["notes"].append("Notes/Extra.md")
        cr = self.mods()[2]
        self.assertEqual([b[0] for b in cr.check()], ["Notes/Extra.md"])

    # -- capture ---------------------------------------------------------
    def _transcript(self, n):
        t = self.tmp / "t.jsonl"
        with t.open("w", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps({"type": "user", "message": {"role": "user", "content": f"ask {i}"}}) + "\n")
                f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": f"answer {i}"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": f"C:/r{i}.py"}}]}}) + "\n")
        return t

    def test_capture_threshold_folders_caps_and_wording_come_from_data(self):
        t = self._transcript(10)
        ev = {"session_id": "abcdef1234", "transcript_path": str(t), "cwd": "C:/x/proj", "hook_event_name": "Stop"}
        cap = self.mods()[3]
        self.assertEqual(cap.handle(ev), "below threshold")  # shipped: 40,000 tokens
        c = self.cfg["capture"]
        c.update(every_tokens=10, sessions_dir="Saved", state_file="Saved/state.json", max_asks=2,
                 edit_tools=["Read"], asked_heading="## Questions", id_chars=4,
                 handoff_line="captured {id} ({reason})", reasons={"Stop": "every {every} tok"})
        _, h, _, cap, _ = self.mods()
        self.assertTrue(cap.handle(ev).startswith("saved"))
        note = next((self.v / "Saved").glob("*abcd.md")).read_text(encoding="utf-8")
        self.assertIn("## Questions\n- ask 8\n- ask 9\n", note)
        self.assertNotIn("- ask 7", note)
        self.assertIn("`C:/r3.py`", note)
        self.assertIn("name: Session-abcd\n", note)
        self.assertIn("captured abcd (every 10 tok)", h.path().read_text(encoding="utf-8"))

    def test_capture_env_override_names_its_variable_in_data(self):
        self.cfg["capture"]["every_tokens_env"] = "MY_CAPTURE_TOKENS"
        with mock.patch.dict(os.environ, {"MY_CAPTURE_TOKENS": "7"}):
            cap = self.mods()[3]
        self.assertEqual(cap.CAPTURE_EVERY_TOKENS, 7)

    # -- snapshot --------------------------------------------------------
    def _snapshot(self):
        snap = self.mods()[4]
        review = self.tmp / "review"
        review.mkdir(exist_ok=True)
        snap.REVIEW = review
        snap.HOME = self.tmp / "home"
        return snap, review

    def test_usage_rows_and_words_come_from_data(self):
        self.cfg["snapshot"]["usage"] = [{"lane": "codex", "label": "Codex X", "plan": "p", "slots": True}]
        self.cfg["snapshot"]["usage_status"]["available"] = "free to work"
        snap, _ = self._snapshot()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            rows = snap._usage([])
        self.assertEqual([(r["model"], r["status"]) for r in rows], [("Codex X", "free to work")])

    def test_activity_tasks_and_waiting_come_from_data(self):
        c = self.cfg["snapshot"]
        c.update(activity_days=3, activity_lanes=["copilot"], title_chars=5)
        c["waiting"].update(section="Blocked", skip=["LATER"], draft="OK? {task}")
        w(self.v, "hot.md", "## Blocked\n- LATER\n- sign the form\n")
        snap, review = self._snapshot()
        w(review, "queue/t1.md", "# A long task title\nQueue: draft\n")
        act = snap._activity()
        self.assertEqual({r["lane"] for r in act}, {"copilot"})
        self.assertEqual(len(act), 3)
        tasks = snap._task_rows()
        self.assertEqual(tasks[0]["task"], "A lon")
        self.assertEqual([r["item"] for r in snap._waiting(tasks)], ["sign the form", "OK? A lon"])

    def test_agents_and_probes_come_from_data(self):
        c = self.cfg["snapshot"]
        c["agents"] = [{"agent": "Tool", "login_file": ".tool/key", "status": "ready", "missing": "log in", "role": "r"},
                       {"agent": "Tray", "status": "{tray}", "role": "t"}]
        c["tray"]["yes"] = "up"
        snap, _ = self._snapshot()
        fake = subprocess.CompletedProcess([], 0, stdout=c["tray"]["image"], stderr="")
        with mock.patch.object(snap.subprocess, "run", return_value=fake):
            rows = snap._agents()
            self.assertEqual([(r["agent"], r["status"]) for r in rows], [("Tool", "log in"), ("Tray", "up")])
            w(self.tmp / "home", ".tool/key", "x")
            self.assertEqual(snap._agents()[0]["status"], "ready")

    def test_hub_folder_comes_from_data_and_keeps_its_ten_tables(self):
        self.cfg["snapshot"]["paths"]["hub"] = "elsewhere"
        snap, review = self._snapshot()
        with mock.patch.object(snap.subprocess, "run", side_effect=OSError("no shell")), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            out = snap.write()
        self.assertEqual(out, review / "elsewhere")
        self.assertEqual(sorted(p.stem for p in out.glob("*.json")),
                         sorted(["agents", "tasks", "waiting", "log", "learner", "spend", "running", "usage",
                                 "outputs", "activity"]))

    # -- hook installer --------------------------------------------------
    def test_hook_events_and_timeout_come_from_data(self):
        self.cfg["hook"].update(events={"Stop": "", "Notification": "*"}, timeout=5)
        self.cfg_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        home = self.tmp / "home"
        home.mkdir()
        env = dict(os.environ, DENISSE_HOME=str(home))
        subprocess.run([sys.executable, str(ROOT / "atrium" / "install_capture.py")], env=env, check=True,
                       capture_output=True, timeout=60)
        hooks = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(sorted(hooks), ["Notification", "Stop"])
        self.assertEqual(hooks["Notification"][0]["matcher"], "*")
        self.assertEqual(hooks["Stop"][0]["hooks"][0]["timeout"], 5)
        self.assertIn("atrium/hook.py", hooks["Stop"][0]["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
