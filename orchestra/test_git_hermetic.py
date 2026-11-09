"""No temp-repo test fixture can reach a real repo through an inherited GIT_* variable.

On 9 Nov 2026 a lane session carried GIT_DIR into the suite, and a fixture's "git init /
config / commit in a temp dir" set core.bare=true and a fake [user] in the main checkout's
.git/config, and made a branch and two commits there. Every fixture that runs git in a temp
folder now starts with testing_git.hermetic_git(self). This module points GIT_DIR,
GIT_WORK_TREE and GIT_INDEX_FILE at a sentinel repo, runs one test of each such fixture in a
child process that inherits them (the way a lane's environment reaches the suite), and checks
the test passed and every byte of the sentinel (config, HEAD, refs, index, hooks, files) is
unchanged.
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.append(str(ROOT))  # testing_git; appended so it never shadows an orchestra module
import testing_git  # noqa: E402

# (folder the test runs from, unittest id): one test per fixture class that runs git in a temp
# repo, picked to exercise its git setup cheaply. RunnerTest's setUp is also the one
# test_autopilot, test_power and test_visible reuse. A new temp-repo fixture belongs here.
FIXTURES = [
    (HERE, "test_runner.RunnerTest.test_task_without_sidecar_is_not_premise_checked"),
    (HERE, "test_runner.PrePushTest.test_push_from_the_main_checkout_is_allowed"),
    (HERE, "test_integration.IntegrationTest.test_master_is_fast_forwarded_to_the_tested_merge_commit"),
    (HERE, "test_crash_recovery.CrashRecoveryTest.test_git_output_is_utf8_whatever_the_console_code_page"),
    (HERE, "test_conflicts.ConflictsTest.test_paths_in_text"),
    (HERE, "test_campaign.RunnerCampaignTest.test_runner_config_follows_the_fake_clock"),
    (HERE, "test_supervision.SupervisionTest.test_lock_records_lane_pid_and_job"),
    (HERE, "test_config.ConfigTest.test_review_risky_patterns_and_max_lines"),
    (HERE, "test_tools.ToolsTest.test_merge_only_from_newest_archive"),
    (HERE, "test_aftercare.TestAftercare.test_vault_note_appends_and_commits"),
    (HERE, "test_aftercare.TestAftercare.test_privacy_check"),
    (HERE, "test_aftercare.TestWaitingList.test_only_hot_md_is_committed"),
    (ROOT / "scripts", "test_vault_git.VaultGitTests.test_checkpoint_preserves_other_staged_work_and_uses_repo_identity"),
    (ROOT / "scripts", "test_nightly_chat_import.NightlyChatImportTest.test_commit_stages_only_new_notes_and_state"),
    (ROOT / "scripts", "test_standing_instructions.ToolTests.test_project_snapshot_is_read_only_in_a_real_temporary_repo"),
    (ROOT, "atrium.test_digest.DigestTest.test_only_the_two_notes_are_committed"),
    (ROOT, "atrium.test_cursors.CursorsTest.test_nothing_is_written"),
    (ROOT, "keats.test_pull.PullTests.test_git_handoff_and_owned_files_preserve_staged_work"),
    (ROOT, "tutor.test_don.GitTests.test_every_write_committed_only_its_files"),
]
# A whole fixture test in a child process; RunnerTest-style ones run a fake lane.
CHILD_TIMEOUT_S = 300


def snapshot(root: Path) -> dict:
    """Every file under root (work tree and .git) -> sha1 of its bytes."""
    return {p.relative_to(root).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


class GitHermeticTest(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)  # the sentinel itself is built hermetically
        self.tmp = Path(tempfile.mkdtemp(prefix="git-sentinel-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.sentinel = testing_git.init_repo(self.tmp / "sentinel", "-b", "sentinel-main",
                                              name="sentinel", email="sentinel@example.invalid")
        (self.sentinel / "s.txt").write_text("sentinel\n", encoding="utf-8")
        g = lambda *a: subprocess.run(["git", *a], cwd=self.sentinel, check=True, capture_output=True)
        g("add", "s.txt")
        g("commit", "-qm", "sentinel")
        self.before = snapshot(self.sentinel)
        git_dir = self.sentinel / ".git"
        self.leak = {"GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(self.sentinel),
                     "GIT_INDEX_FILE": str(git_dir / "index")}

    def leaked_env(self) -> dict:
        """The caller's environment as a careless lane would pass it: sentinel GIT_* and the real
        machine config (the hermetic overrides are what each fixture must put back itself)."""
        env = {k: v for k, v in os.environ.items() if not testing_git.is_git_var(k)}
        env.update(self.leak, PYTHONIOENCODING="utf-8")
        return env

    def assertSentinelUntouched(self, who: str):
        after = snapshot(self.sentinel)
        changed = sorted(k for k in set(self.before) | set(after) if self.before.get(k) != after.get(k))
        self.assertEqual(changed, [], f"{who} changed the sentinel repo the leaked GIT_DIR pointed at")

    def test_hermetic_git_drops_every_git_var_and_restores_them(self):
        with mock.patch.dict(os.environ, {**self.leak, "GIT_AUTHOR_NAME": "x"}):
            case = unittest.TestCase()
            config = testing_git.hermetic_git(case)
            self.assertEqual([k for k in os.environ if k in self.leak or k == "GIT_AUTHOR_NAME"], [])
            self.assertEqual(os.environ["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(config.read_text(encoding="utf-8"), "", "the global config is empty")
            repo = testing_git.init_repo(self.tmp / "inner", "-b", "main")
            got = subprocess.run(["git", "config", "--show-origin", "user.name"], cwd=repo,
                                 capture_output=True, text=True, check=True).stdout
            self.assertIn(testing_git.NAME, got)
            self.assertIn(".git/config", got.replace("\\", "/"), "the identity is the temp repo's own")
            case.doCleanups()
            self.assertEqual(os.environ["GIT_DIR"], self.leak["GIT_DIR"], "restored afterwards")
            self.assertFalse(config.exists())
        self.assertSentinelUntouched("hermetic_git")

    def test_init_repo_refuses_when_a_git_dir_is_leaked(self):
        with mock.patch.dict(os.environ, self.leak):
            with self.assertRaises(AssertionError) as e:
                testing_git.init_repo(self.tmp / "unsafe", "-b", "main")
            self.assertIn("GIT_DIR", str(e.exception))
            with self.assertRaises(AssertionError):
                testing_git.identity(self.sentinel)
        self.assertFalse((self.tmp / "unsafe").exists(), "refused before git init")
        self.assertSentinelUntouched("init_repo")

    def test_every_temp_repo_fixture_leaves_a_leaked_git_dir_alone(self):
        env = self.leaked_env()

        def run(spec):
            cwd, test_id = spec
            try:
                r = subprocess.run([sys.executable, "-m", "unittest", test_id], cwd=cwd, env=env,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=CHILD_TIMEOUT_S)
                return test_id, r.returncode, (r.stdout + r.stderr)[-3000:]
            except subprocess.TimeoutExpired:
                return test_id, "timeout", ""

        with ThreadPoolExecutor(max(1, min(len(FIXTURES), os.cpu_count() or 1))) as pool:
            results = list(pool.map(run, FIXTURES))
        for test_id, code, out in results:
            with self.subTest(test_id):
                self.assertEqual(code, 0, f"{test_id} failed with GIT_DIR leaked:\n{out}")
                self.assertRegex(out, r"Ran 1 test", f"{test_id}: the id no longer names a test")
        self.assertSentinelUntouched("a fixture")


if __name__ == "__main__":
    unittest.main()
