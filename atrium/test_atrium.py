"""python -m unittest atrium.test_atrium -v  (temp vault)"""
import datetime as dt
import importlib
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

    def test_credits_flags_untagged_decisions(self):
        bad = self.cr.check()
        self.assertEqual(len(bad), 1)
        self.assertIn("untagged decision", bad[0][2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
