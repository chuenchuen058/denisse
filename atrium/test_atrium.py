"""python -m unittest atrium.test_atrium -v  (temp vault)"""
import datetime as dt
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path


def w(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class AtriumTest(unittest.TestCase):
    def setUp(self):
        self.v = Path(tempfile.mkdtemp())
        os.environ["DENISSE_VAULT"] = str(self.v)
        from atrium import context, credits
        self.ctx = importlib.reload(context)
        self.cr = importlib.reload(credits)
        today = dt.date.today().isoformat()
        w(self.v, "hot.md", "---\nname: hot\n---\n# Hot\n\n## Active right now\n- 2026-11-01 [Chuen] Voice is local\n\n## Waiting on Chuen\n- approve draft task\n\n## Open hazards\n- x\n")
        w(self.v, "Atlas/Standing-Asks.md", "---\nname: Standing-Asks\n---\n# Standing asks\n\n- 2026-10-27 [Chuen] Commit and push\n")
        w(self.v, "Atlas/Settled.md", "---\nname: Settled\n---\n\n" + "".join(f"- 2026-10-{i:02d} [Claude] fact {i}\n" for i in range(1, 21)))
        w(self.v, "Atlas/Concepts/Learning/Learner-Model.md", "---\nname: Learner-Model\n---\n## Before planning anything about Chuen\n\n### Format\n- Worked example first _(answered)_\n### Study\n- unknown\n\n## Bank\n- 3 open\n")
        w(self.v, f"Calendar/Handoff-{today}.md", "## Log\n- 09:00 [codex] did a thing\n- 09:05 untagged decision\n")

    def test_pack_has_live_state_and_drops_unknowns(self):
        out = self.ctx.show("claude")
        for s in ("Commit and push", "Worked example first", "Voice is local", "approve draft task", "fact 20", "did a thing"):
            self.assertIn(s, out)
        self.assertNotIn("- unknown", out)
        self.assertNotIn("fact 1\n", out)  # only the latest 12 settled lines
        self.assertNotIn("Open hazards", out)
        self.assertNotIn("### Study", out)  # an area with nothing known is left out
        self.assertIn("### Format", out)

    def test_budget_is_respected(self):
        w(self.v, "STATE NOW.md", "## STATE NOW\n" + "x" * 9000)
        self.assertEqual(self.ctx.show("claude").count("## STATE NOW"), 1)
        out = self.ctx.show("copilot")
        self.assertLess(len(out), self.ctx.BUDGET["copilot"] + 300)

    def test_missing_vault_is_fine(self):
        os.environ["DENISSE_VAULT"] = str(self.v / "nope")
        importlib.reload(self.ctx)
        self.assertIn("Shared context", self.ctx.show("codex"))

    def test_handoff_append_is_tagged_and_creates_the_note(self):
        from atrium import handoff
        h = importlib.reload(handoff)
        today = dt.date.today().isoformat()
        (self.v / "Calendar" / f"Handoff-{today}.md").unlink()
        self.assertTrue(h.remember("denisse", ["handed  CV draft\nto Claude", ""]))
        text = h.path().read_text(encoding="utf-8")
        self.assertTrue(text.startswith(f"---\nname: Handoff-{today}\ndescription: "))
        self.assertRegex(text, r"- \d\d:\d\d \[denisse\] handed CV draft to Claude\n")
        os.environ["DENISSE_VAULT"] = str(self.v / "nope")
        importlib.reload(self.ctx); h = importlib.reload(handoff)
        self.assertFalse(h.remember("denisse", ["x"]))

    def test_capture_waits_for_the_threshold_then_saves_and_logs(self):
        from atrium import capture, handoff
        importlib.reload(handoff); cap = importlib.reload(capture)
        cap.CAPTURE_EVERY_TOKENS = 100  # 400 bytes
        t = self.v / "t.jsonl"
        def say(n):
            with t.open("a", encoding="utf-8") as f:
                for i in range(n):
                    f.write(json.dumps({"type": "user", "message": {"role": "user", "content": f"please fix the tray bug number {i}"}}) + "\n")
                    f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                        {"type": "text", "text": f"Fixed bug {i}."},
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "C:/x/tray.py"}}]}}) + "\n")
        ev = {"session_id": "abcdef1234", "transcript_path": str(t), "cwd": "C:/Users/Xichu/GitHub/denisse", "hook_event_name": "Stop"}
        say(1)
        self.assertEqual(cap.handle(ev), "below threshold")
        say(5)
        self.assertTrue(cap.handle(ev).startswith("saved"))
        note = next((self.v / "Calendar" / "Sessions").glob("*abcdef12.md")).read_text(encoding="utf-8")
        self.assertTrue(note.startswith("---\nname: Session-abcdef12\ndescription: "))
        self.assertIn("please fix the tray bug number 4", note)
        self.assertIn("`C:/x/tray.py`", note)
        self.assertEqual(cap.handle(ev), "below threshold")  # nothing new since
        self.assertTrue(cap.handle(dict(ev, hook_event_name="PreCompact")).startswith("saved"))  # always
        self.assertEqual(len(list((self.v / "Calendar" / "Sessions").glob("*.md"))), 1)  # one note per session
        self.assertIn("session abcdef12 in denisse saved", handoff.path().read_text(encoding="utf-8"))

    def test_credits_flags_untagged_decisions(self):
        bad = self.cr.check()
        self.assertEqual(len(bad), 1)
        self.assertIn("untagged decision", bad[0][2])

    def test_usage_rows_carry_exam_pass_rate(self):
        from atrium import snapshot
        review = Path(tempfile.mkdtemp())
        old = snapshot.REVIEW
        snapshot.REVIEW = review
        from unittest import mock
        offline = mock.patch("urllib.request.urlopen", side_effect=OSError("no Ollama in tests"))
        offline.start()
        try:
            self.assertIsNone(next(r for r in snapshot._usage([]) if r["lane"] == "codex")["exam_pass_rate"])
            ok, bad = {"at": "2026-11-03T07:10:00", "ok": True}, {"at": "2026-11-03T07:11:00", "ok": False}
            w(review, "exams/scores.json", json.dumps({"agents": {
                "codex": {"extract": [ok, ok, bad, ok]},
                "local:llama3.1:8b": {"extract": [ok]}, "local:tiny": {"extract": [bad]}}}))
            rows = {r["lane"]: r for r in snapshot._usage([])}
            self.assertEqual(rows["codex"]["exam_pass_rate"], 0.75)
            self.assertEqual(rows["local"]["exam_pass_rate"], 0.5)
            self.assertIsNone(rows["copilot"]["exam_pass_rate"])
        finally:
            offline.stop()
            snapshot.REVIEW = old

    def test_usage_takes_lane_limits_from_the_runner_record_across_midnight(self):
        # 11 Nov 2026: a Codex limit hit the day before (until 12:08 next day) showed "available"
        from atrium import snapshot
        from unittest import mock
        review = Path(tempfile.mkdtemp())
        old = snapshot.REVIEW
        snapshot.REVIEW = review
        now = dt.datetime.now().replace(second=0, microsecond=0)
        later, past = now + dt.timedelta(days=1, hours=2), now - dt.timedelta(minutes=5)
        w(review, "state.json", json.dumps({"tasks": {}, "lanes": {
            "codex": {"limited_until": later.isoformat(), "since": (now - dt.timedelta(hours=20)).isoformat()},
            "claude": {"limited_until": past.isoformat()}}}))
        offline = mock.patch("urllib.request.urlopen", side_effect=OSError("no Ollama in tests"))
        offline.start()
        try:
            rows = {r["lane"]: r for r in snapshot._usage([])}
        finally:
            offline.stop()
            snapshot.REVIEW = old
        self.assertTrue(rows["codex"]["status"].startswith("limited until "), rows["codex"]["status"])
        self.assertIn(later.strftime("%H:%M"), rows["codex"]["status"])
        self.assertEqual(rows["codex"]["state"], "bad")
        self.assertEqual(dt.datetime.fromisoformat(rows["codex"]["limited_until"]).astimezone().replace(tzinfo=None), later)
        self.assertEqual(rows["claude"]["status"], "available")  # that limit has lifted
        self.assertEqual(rows["claude"]["limited_until"], "")
        self.assertEqual(rows["gemini"]["limited_until"], "")  # a fixed status never counts down

    def test_waiting_drops_a_lane_limit_line_once_it_has_lifted(self):
        from atrium import snapshot
        now = dt.datetime.now().replace(second=0, microsecond=0)
        soon, gone = now + dt.timedelta(hours=3), now - dt.timedelta(hours=1)
        w(self.v, "hot.md", "## Waiting on Chuen\n"
          f"- Lane codex limited until {soon:%Y-%m-%d %H:%M}\n- Lane claude limited until {gone:%Y-%m-%d %H:%M}\n- reply to Terry\n")
        rows = snapshot.waiting_items([])
        self.assertEqual([r["item"] for r in rows], [f"Lane codex limited until {soon:%Y-%m-%d %H:%M}", "reply to Terry"])
        self.assertEqual(dt.datetime.fromisoformat(rows[0]["until"]).astimezone().replace(tzinfo=None), soon)
        self.assertNotIn("until", rows[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
