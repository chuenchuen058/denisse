"""scripts/link_related_logs.py against a throwaway fixture vault and backup folder, never the real ones."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "orchestra"))
import link_related_logs as lr  # noqa: E402
import vaultlock  # noqa: E402

CFG = lr.CFG
START, END = CFG["block_start"], CFG["block_end"]
ROOTS = CFG["log_roots"]
# Folders the pass must never link to, whatever the logs say (they are in skip_roots).
NEVER = ("Denisse/Brain", "Denisse/Evals")

NOTES = {
    "Atlas/Academic/Physics/Thermodynamics.md": "Heat engines and entropy.",
    "Atlas/Projects/Rover/Rover Chassis.md": "Aluminium frame for the rover.",
    "Dev/Tools/Obsidian Graph.md": "How the graph view is coloured.",
    "Atlas/Clubs/Chess Society.md": "Weekly chess meetings.",
    "Denisse/Brain/Learned Facts.md": "Facts Denisse learned.",
    "Denisse/Evals/Eval Cases.md": "The eval suite cases.",
}


def log(title, body, nl="\n"):
    return nl.join(["---", f"name: {title}", "type: chat", "tags: [chat]", "---", "",
                    f"# {title}", "", body, ""])


LOGS = {
    f"{ROOTS[0]}/2026-11/2026-11-10 chat thermo.md":
        log("thermo", "We revised Thermodynamics, then more Thermodynamics: entropy of heat engines, Carnot cycle efficiency. "
                      "Also checked Learned Facts and Eval Cases."),
    f"{ROOTS[0]}/2026-11/2026-11-11 chat thermo again.md":
        log("thermo again", "More Thermodynamics questions about entropy and Carnot efficiency, "
                            "the Learned Facts file and Eval Cases."),
    f"{ROOTS[1]}/2026-11/2026-11-12 session rover.md":
        log("rover", "Designed the Rover Chassis: aluminium extrusions, bolts, motor mounts.", nl="\r\n"),
    f"{ROOTS[1]}/2026-11/2026-11-13 session rover two.md":
        log("rover two", "Rover Chassis motor mounts and aluminium extrusions revised."),
    f"{ROOTS[2]}/2026-11/2026-11-14 orchestra graph.md":
        log("graph", "Fixed the Obsidian Graph colour groups and topic fields."),
    f"{ROOTS[3]}/2026-11/2026-11-15 log chess.md":
        log("chess", "Planned the Chess Society tournament brackets and pairings."),
    f"{ROOTS[0]}/2026-11/2026-11-15 chat nothing.md":
        log("nothing", "Small talk about lunch and weather."),
    f"{ROOTS[0]}/2026-11/2026-11-16 chat weather.md":
        log("weather", "Rain forecast for the weekend, umbrella needed."),
}


class RelatedLinksTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="related_links_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.vault, self.backups, self.lockfile = tmp / "vault", tmp / "backups", tmp / "vault.lock"
        for rel, text in {**NOTES, **LOGS}.items():
            self.write(rel, text)
        self.write(".obsidian/graph.md", "Thermodynamics Rover Chassis")

    def write(self, rel, text):
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))

    def read(self, rel):
        return (self.vault / rel).read_bytes().decode("utf-8")

    def snapshot(self):
        return {p.relative_to(self.vault).as_posix(): p.read_bytes()
                for p in self.vault.rglob("*") if p.is_file() and ".git" not in p.parts}

    def run_pass(self, **kw):
        return lr.run(self.vault, backups=self.backups, lock=vaultlock.VaultLock(self.lockfile, wait=5),
                      say=lambda *_: None, **kw)

    # -- the block -------------------------------------------------------------
    def test_block_added_once_with_links_and_topic(self):
        out = self.run_pass()
        self.assertTrue(out["changed"])
        thermo = self.read(f"{ROOTS[0]}/2026-11/2026-11-10 chat thermo.md")
        self.assertEqual(thermo.count(START), 1)
        self.assertEqual(thermo.count(END), 1)
        self.assertIn("[[Atlas/Academic/Physics/Thermodynamics|Thermodynamics]]", thermo)
        self.assertIn(f"{CFG['topic_field']}:: study", thermo)
        self.assertTrue(thermo.rstrip().endswith(END))
        rover = self.read(f"{ROOTS[1]}/2026-11/2026-11-12 session rover.md")
        self.assertIn("[[Atlas/Projects/Rover/Rover Chassis|Rover Chassis]]", rover)
        self.assertNotRegex(rover.replace("\r\n", ""), "\n")  # a CRLF log keeps CRLF in its block
        for rel in out["changed"]:
            self.assertEqual(self.read(rel).count(START), 1, rel)
        self.assertFalse(self.lockfile.exists())

    def test_rerun_is_a_noop(self):
        self.run_pass()
        before = self.snapshot()
        mtimes = {r: (self.vault / r).stat().st_mtime_ns for r in before}
        backups = sorted(p.name for p in self.backups.iterdir())
        out = self.run_pass()
        self.assertEqual(out["changed"], [])
        self.assertIsNone(out["backup"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual({r: (self.vault / r).stat().st_mtime_ns for r in before}, mtimes)
        self.assertEqual(sorted(p.name for p in self.backups.iterdir()), backups)
        for rel in LOGS:
            self.assertLessEqual(self.read(rel).count(START), 1, rel)

    def test_own_text_never_changes_outside_the_block(self):
        stale = (f"{ROOTS[0]}/2026-11/2026-11-09 chat stale block.md")
        self.write(stale, log("stale", "Thermodynamics entropy notes.") +
                   f"\n{START}\n{CFG['heading']}\n- [[Nowhere]]\n{END}\nWritten after the block by hand.\n")
        original = {r: self.read(r) for r in [*LOGS, stale]}
        self.run_pass()
        self.run_pass()
        for rel, old in original.items():
            new = self.read(rel)
            self.assertEqual(lr.strip_block(new).rstrip(), lr.strip_block(old).rstrip(), rel)
            if START not in old:
                if START in new:
                    self.assertTrue(new.startswith(old.rstrip("\r\n")), rel)
                else:
                    self.assertEqual(new, old, rel)  # nothing to add: not touched at all
        self.assertIn("Written after the block by hand.", self.read(stale))
        self.assertNotIn("[[Nowhere]]", self.read(stale))
        # everything that is not a log is byte-for-byte what it was
        for rel, text in {**NOTES, ".obsidian/graph.md": "Thermodynamics Rover Chassis"}.items():
            self.assertEqual(self.read(rel), text, rel)

    def test_brain_and_evals_are_never_targets(self):
        notes = lr.load(self.vault)
        targets = lr.targets_and_phrases(notes)
        self.assertIn("Atlas/Academic/Physics/Thermodynamics.md", targets)
        for rel in targets:
            self.assertFalse(rel.startswith(tuple(n + "/" for n in NEVER)), rel)
        _, out = lr.plan(self.vault)
        for rel, info in out.items():
            for t in info["links"] + info.get("similar", []):
                self.assertFalse(t.startswith(tuple(n + "/" for n in NEVER)), (rel, t))
        self.run_pass()
        for rel in LOGS:
            text = self.read(rel)
            self.assertNotIn("[[Denisse/Brain", text)
            self.assertNotIn("[[Denisse/Evals", text)
        self.assertNotIn(".obsidian/graph.md", out)

    def test_dry_run_writes_nothing(self):
        before = self.snapshot()
        out = self.run_pass(apply=False)
        self.assertTrue(out["changed"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.backups.exists())
        self.assertFalse(self.lockfile.exists())

    # -- backups -----------------------------------------------------------------
    def test_backup_holds_the_old_text_and_undo_manifest(self):
        original = {r: self.read(r) for r in LOGS}
        out = self.run_pass()
        man = Path(out["backup"])
        self.assertTrue(man.is_file())
        bdir = man.parent
        self.assertTrue(bdir.name.startswith(CFG["backup_prefix"]))
        for rel in out["changed"]:
            self.assertEqual((bdir / rel).read_bytes().decode("utf-8"), original[rel])

    def test_old_backups_move_to_trash_never_deleted(self):
        keep = CFG["keep_backups"]
        self.backups.mkdir()
        names = [f"{CFG['backup_prefix']}2026-11-{d:02d}T000000" for d in range(1, keep + 4)]
        for n in names:
            (self.backups / n).mkdir()
            (self.backups / n / "manifest.json").write_text("{}", encoding="utf-8")
        (self.backups / "someone-else-2026-01-01").mkdir()
        out = self.run_pass()
        ours = sorted(p.name for p in self.backups.iterdir() if p.name.startswith(CFG["backup_prefix"]))
        self.assertEqual(len(ours), keep)
        self.assertIn(Path(out["backup"]).parent.name, ours)  # tonight's is kept
        trash = self.backups / CFG["trash_dir"]
        moved = sorted(p.name for p in trash.iterdir())
        self.assertEqual(moved, names[:4])
        self.assertEqual(sorted(out["pruned"]), names[:4])
        for n in moved:
            self.assertTrue((trash / n / "manifest.json").is_file())
        self.assertTrue((self.backups / "someone-else-2026-01-01").is_dir())

    def test_trash_name_clash_is_kept_side_by_side(self):
        self.backups.mkdir()
        old = f"{CFG['backup_prefix']}2000-01-01T000000"
        (self.backups / CFG["trash_dir"] / old).mkdir(parents=True)
        (self.backups / old).mkdir()
        lr.prune_backups(self.backups, keep=0)
        self.assertTrue((self.backups / CFG["trash_dir"] / old).is_dir())
        self.assertTrue((self.backups / CFG["trash_dir"] / f"{old}-2").is_dir())

    # -- commit --------------------------------------------------------------------
    def git(self, *args):
        return subprocess.run(("git", *args), cwd=self.vault, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=True).stdout

    def test_commits_only_the_logs_it_changed(self):
        if not shutil.which("git"):
            self.skipTest("git not on PATH")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")
        self.git("config", "core.autocrlf", "false")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "fixture")
        # someone else's work: an edited note left unstaged, a new note staged
        self.write("Atlas/Clubs/Chess Society.md", "Edited by hand, not committed.")
        self.write("Dev/staged.md", "staged by someone else")
        self.git("add", "Dev/staged.md")
        out = self.run_pass(commit=True)
        self.assertNotIn(out["commit"], ("none", "failed"))
        committed = set(self.git("show", "--name-only", "--format=", "HEAD").splitlines())
        self.assertEqual(committed, set(out["changed"]))
        status = self.git("status", "--porcelain")
        self.assertIn("A  Dev/staged.md", status)
        self.assertRegex(status, r' M "?Atlas/Clubs/Chess Society.md')
        self.assertEqual(self.run_pass(commit=True)["commit"], "none")

    def test_nightly_step_never_raises(self):
        msgs = []
        res = lr.nightly_step(self.vault / "missing", msgs.append, apply=True, commit=False,
                              backups=self.backups)
        self.assertEqual(res["changed"], [])
        self.assertTrue(msgs and msgs[0].startswith("=="))

    def test_nightly_runs_it_after_topic_hubs(self):
        src = (HERE / "nightly.py").read_text(encoding="utf-8")
        hubs = src.index("topic_hubs.nightly_step(")
        rel = src.index("link_related_logs.nightly_step(")
        self.assertLess(hubs, rel)
        self.assertIn("apply=not DRY, commit=not DRY", src[rel:rel + 120])


if __name__ == "__main__":
    unittest.main()
