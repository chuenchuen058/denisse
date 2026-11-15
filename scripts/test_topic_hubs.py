"""scripts/topic_hubs.py against a throwaway fixture vault, never the real one."""
import copy
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import topic_hubs as th  # noqa: E402

TODAY = dt.date(2026, 11, 15)
CFG = th.load_config()


def chat(name, asked, day="2026-11-10"):
    return (f'---\nname: "{name}"\ndescription: "x"\nupdated: {day}\ntype: chat\ndate: {day}\n'
            f"tags: [chat]\n---\n\n# {name}\n\n## What was asked\n\n- {asked}\n\n## Outcome\n\nok\n")


def session(name, asked):
    return (f"---\nname: Session-{name[-8:]}\ndescription: a session\nupdated: 2026-11-02\n"
            f"type: session\ntags: [session]\n---\n\n# {name}\n\n## Asked\n- {asked}\n\n## Last answers\n- done\n")


def note(name):
    return f"---\nname: {name}\ndescription: anchor\nupdated: 2026-11-01\ntags: [x]\n---\n\n# {name}\n"


class TopicHubsTest(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="topic_hubs_"))
        self.addCleanup(shutil.rmtree, self.vault, True)
        self.lock = self.vault.parent / f"{self.vault.name}.lock"
        self.cfg = copy.deepcopy(CFG)
        # Anchors and the index the hubs link up to; Calendar-Index links Topics (as in the vault).
        self.write("index.md", note("index") + "\n[[Calendar-Index]] [[Orchestra]] [[Don]]\n")
        self.write("Calendar/Calendar-Index.md", note("Calendar-Index") + "\n[[Topics]]\n")
        self.write("Atlas/Orchestra.md", note("Orchestra"))
        self.write("Atlas/Don.md", note("Don"))
        self.write("Calendar/Chats/2026-11/2026-11-10 claude-code You are running one Orchestra task.md",
                   chat("orch", "You are running one Orchestra task, unattended"))
        self.write("Calendar/Chats/2026-11/2026-11-03 claude-code he asked v0.2.1 fix.md",
                   chat("dots", "You are Don, checking Anki cards", "2026-11-03"))
        self.write("Calendar/Chats/2026-10/2026-10-24 codex could you remove the sidepanel.md",
                   chat("else", "could you remove the sidepanel", "2026-10-24"))
        self.write("Calendar/Sessions/2026-11/2026-11-02 2026-11-02T1905-atrium-board 12541099.md",
                   session("2026-11-02T1905-atrium-board 12541099", "build the board"))
        self.write("Calendar/Chats/2026-11/2026-11-04 claude-code fix `brain.py` in [draft].md",
                   chat("ticks", "the \\`code\\` and \\[brackets\\]", "2026-11-04"))

    def write(self, rel, text):
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def hub(self, name):
        return (self.vault / self.cfg["folder"] / f"{name}.md").read_text(encoding="utf-8")

    def run_hubs(self, **kw):
        kw.setdefault("today", TODAY)
        return th.run(self.vault, cfg=self.cfg, lock=th.vaultlock.VaultLock(self.lock, wait=5),
                      say=lambda *_: None, **kw)

    def topic_of(self, rel):
        logs, _ = th.read_logs(self.vault, self.cfg)
        groups = th.classify(logs, self.cfg)
        return next(t for t, items in groups.items() if any(g["rel"] == rel for g in items))

    # -- classification ------------------------------------------------------
    def test_classification_first_match_title_and_asked_then_fallback(self):
        self.assertEqual(self.topic_of("Calendar/Chats/2026-11/2026-11-10 claude-code You are running "
                                       "one Orchestra task.md"), "Orchestra runs")
        self.assertEqual(self.topic_of("Calendar/Chats/2026-11/2026-11-03 claude-code he asked v0.2.1 fix.md"),
                         "Study and Don")  # matched on the asked section only
        self.assertEqual(self.topic_of("Calendar/Sessions/2026-11/2026-11-02 2026-11-02T1905-atrium-board "
                                       "12541099.md"), "Atrium hub")  # title pattern, session format
        self.assertEqual(self.topic_of("Calendar/Chats/2026-10/2026-10-24 codex could you remove the "
                                       "sidepanel.md"), "Everything else")

    def test_topics_come_from_config_not_code(self):
        self.cfg["topics"].insert(0, {"name": "Sidepanels", "anchors": [], "asked": ["sidepanel"]})
        self.assertEqual(self.topic_of("Calendar/Chats/2026-10/2026-10-24 codex could you remove the "
                                       "sidepanel.md"), "Sidepanels")
        src = (HERE / "topic_hubs.py").read_text(encoding="utf-8")
        for topic in CFG["topics"]:
            self.assertNotIn(topic["name"], src)

    # -- the hub files -------------------------------------------------------
    def test_hub_format_links_and_index(self):
        out = self.run_hubs()
        self.assertIn("Calendar/Topics/Topics.md", out["changed"])
        orch = self.hub("Orchestra runs")
        self.assertTrue(orch.startswith("---\nname: Orchestra runs\ndescription: \"Topic hub: every chat"))
        self.assertIn("tags: [folder-hub, topic-hub]\nupdated: 2026-11-15\n", orch)
        # anchors that exist, then the index; Dev-Index isn't in the fixture so it's left out
        self.assertIn("Related: [[Orchestra]] · [[Topics]]\n", orch)
        self.assertIn("- [[Calendar/Chats/2026-11/2026-11-10 claude-code You are running one Orchestra "
                      "task.md|2026-11-10 claude-code You are running one Orchestra task]]", orch)
        index = self.hub("Topics")
        self.assertIn("Back to [[Calendar-Index]]", index)
        for name in ("Atrium hub", "Everything else", "Orchestra runs", "Study and Don"):
            self.assertIn(f"- [[{name}]]", index)
        self.assertNotIn("[[Markets and money]]", index)  # no logs, no hub
        self.assertFalse((self.vault / "Calendar/Topics/Markets and money.md").exists())

    def test_names_with_dots_keep_the_md_extension(self):
        self.run_hubs()
        study = self.hub("Study and Don")
        # without '.md' Obsidian would read 'v0.2.1 fix' as a file with extension '.1 fix'
        self.assertIn("[[Calendar/Chats/2026-11/2026-11-03 claude-code he asked v0.2.1 fix.md|"
                      "2026-11-03 claude-code he asked v0.2.1 fix]]", study)

    def test_aliases_never_hold_backticks_or_brackets(self):
        self.cfg["alias_chars"] = 200
        self.write("Calendar/Chats/2026-11/2026-11-05 codex fix `brain.py` (2).md",
                   chat("tick", "fix the github repo", "2026-11-05"))
        self.run_hubs()
        for f in (self.vault / "Calendar/Topics").iterdir():
            for alias in re.findall(r"\[\[[^|\]]+\|(.*?)\]\]$", f.read_text(encoding="utf-8"), re.M):
                self.assertNotRegex(alias, r"[\[\]`|]", f.name)

    def test_unlinkable_names_are_skipped_not_broken(self):
        out = self.run_hubs()
        # '[draft]' in the FILE name: a wikilink target can't hold ']' so it is reported, not linked
        self.assertIn("Calendar/Chats/2026-11/2026-11-04 claude-code fix `brain.py` in [draft].md", out["skipped"])
        self.write("Calendar/Chats/2026-11/2026-11-05 codex fix `brain.py` now.md",
                   chat("tick", "fix the github repo", "2026-11-05"))
        self.run_hubs()
        line = next(l for l in self.hub("GitHub and coding").splitlines() if "brain.py" in l)
        self.assertEqual(line, "- [[Calendar/Chats/2026-11/2026-11-05 codex fix `brain.py` now.md|"
                               "2026-11-05 codex fix brain.py now]]")

    def test_weekly_split_over_threshold(self):
        self.cfg["split_over"] = 2
        self.write("Calendar/Chats/2026-11/2026-11-04 codex Orchestra again.md",
                   chat("o2", "running one Orchestra task", "2026-11-04"))
        self.write("Calendar/Chats/2026-10/2026-10-28 codex Orchestra earlier.md",
                   chat("o3", "running one Orchestra task", "2026-10-28"))
        self.run_hubs()
        top = self.hub("Orchestra runs")
        self.assertIn("split by week", top)
        self.assertIn("## Weeks\n\n- [[Orchestra 2026-W46]]\n- [[Orchestra 2026-W45]]\n- [[Orchestra 2026-W44]]\n",
                      top)
        self.assertNotIn("Calendar/Chats", top)
        w46 = self.hub("Orchestra 2026-W46")
        self.assertIn('description: "Orchestra logs, week 2026-W46 (1)."', w46)
        self.assertIn("Related: [[Orchestra runs]]\n", w46)
        self.assertIn("2026-11-10 claude-code You are running one Orchestra task.md|", w46)
        self.assertIn("2026-10-28 codex Orchestra earlier.md|", self.hub("Orchestra 2026-W44"))

    # -- idempotence ---------------------------------------------------------
    def test_rerun_rewrites_nothing_and_keeps_dates(self):
        self.run_hubs()
        before = {p: p.read_bytes() for p in (self.vault / "Calendar/Topics").iterdir()}
        mtimes = {p: p.stat().st_mtime_ns for p in before}
        out = self.run_hubs(today=TODAY + dt.timedelta(days=3))
        self.assertEqual(out["changed"], [])
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual({p: p.stat().st_mtime_ns for p in before}, mtimes)
        # a new log changes only its own hub and the counts that mention it
        self.write("Calendar/Chats/2026-11/2026-11-14 codex Orchestra later.md",
                   chat("o4", "running one Orchestra task", "2026-11-14"))
        out = self.run_hubs(today=TODAY + dt.timedelta(days=3))
        self.assertEqual(out["changed"], ["Calendar/Topics/Orchestra runs.md"])
        self.assertIn("updated: 2026-11-18", self.hub("Orchestra runs"))
        self.assertIn("updated: 2026-11-15", self.hub("Study and Don"))

    def test_dry_run_writes_nothing(self):
        out = self.run_hubs(apply=False)
        self.assertTrue(out["changed"])
        self.assertFalse((self.vault / "Calendar/Topics").exists())
        self.assertFalse(self.lock.exists())

    def test_lock_is_released(self):
        self.run_hubs()
        self.assertFalse(self.lock.exists())

    # -- the vault contract ----------------------------------------------------
    def test_vault_contract_clean(self):
        self.write("Calendar/Chats/2026-11/2026-11-06 codex v1.2 tail_mech fix.md",
                   chat("dots2", "BlueBand pcb", "2026-11-06"))
        self.run_hubs()
        env = dict(os.environ, DENISSE_VAULT=str(self.vault))
        res = subprocess.run([sys.executable, str(HERE / "vault_contract.py")], env=env,
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        topics = [l for l in res.stdout.splitlines() if "Calendar/Topics/" in l]
        self.assertEqual(topics, [], res.stdout)
        # session logs aren't 'generated', so the contract checks them for orphans: the hubs link them
        self.assertNotRegex(res.stdout, r"Calendar/Sessions/.*orphan")
        self.assertFalse(re.search(r"ERROR .*broken link", res.stdout), res.stdout)


if __name__ == "__main__":
    unittest.main()
