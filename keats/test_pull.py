"""Synthetic Moodle pages and disposable vaults only; safe under run_checks."""
from contextlib import contextmanager
import copy
import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from keats import conf, pull
from keats.ingest import commands
from keats.scan import LoginRequired, Resource, Response, discover, filename, resolve

FIXTURES = Path(__file__).with_name("fixtures")


class FakeBrowser:
    def __init__(self, config):
        self.base = config["browser"]["base_url"].rstrip("/")
        self.downloaded = []
        self.fetched = []
        self.fail_download = None
        self.pages = {
            "/course/view.php?id=101": (FIXTURES / "course.html").read_text(),
            "/course/view.php?id=101&section=2": '<main id="region-main"><section id="section-2">'
                '<h3 class="sectionname">Week 2</h3><a href="/mod/resource/view.php?id=6">Lecture slides</a>'
                '</section></main>',
            "/mod/resource/view.php?id=1": (FIXTURES / "resource.html").read_text(),
            "/mod/folder/view.php?id=3": '<main role="main">'
                '<a href="/pluginfile.php/8/mod_folder/content/0/Answers.pdf?forcedownload=1">Answers</a>'
                '<a href="/pluginfile.php/8/mod_folder/content/0/solver.m?forcedownload=1">MATLAB</a>'
                '</main>',
        }
        for key, name in [(2, "Questions.docx"), (4, "Video.mp4"), (6, "Slides.pptx")]:
            self.pages[f"/mod/resource/view.php?id={key}"] = Response(
                self.base + f"/pluginfile.php/{key}/mod_resource/content/1/{name}",
                {"content-type": "application/octet-stream"})

    def fetch(self, url):
        self.fetched.append(url)
        value = self.pages[url.removeprefix(self.base)]
        return value if isinstance(value, Response) else Response(url, {"content-type": "text/html"}, value)

    def download(self, url, destination):
        if self.fail_download and self.fail_download in url:
            raise OSError("synthetic download failure")
        self.downloaded.append(url)
        Path(destination).write_bytes(b"synthetic nonempty file")

    @contextmanager
    def factory(self, config, **kwargs):
        yield self


class PullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = conf.load()
        self.config["paths"]["profile"] = str(self.root / "profile")
        self.config["browser"]["base_url"] = "https://moodle.invalid/"
        self.manifest = self.vault / self.config["paths"]["manifest"]
        self.manifest.parent.mkdir(parents=True)
        self.module = "TEST101 Example Module"
        self.data = {"courses": {"101": self.module}, "captured": [], "last_sync": "2000-01-01", "unknown": "keep"}
        self.save_manifest()
        self.browser = FakeBrowser(self.config)
        self.recorder, self.notifier, self.ingester = Mock(), Mock(), Mock()

    def save_manifest(self):
        self.manifest.write_text(json.dumps(self.data), encoding="utf-8")

    def run_pull(self, **kwargs):
        return pull.run(self.config, self.vault, browser_factory=self.browser.factory,
                        recorder=self.recorder, notifier=self.notifier, ingester=self.ingester,
                        say=lambda text: None, **kwargs)

    def test_full_pull_repeat_and_names(self):
        result = self.run_pull(ingest=True)
        self.assertEqual(result["count"], 5)
        week = self.manifest.parent / self.module / "Week 1"
        for name in ["Lecture - Notes.pdf", "Tutorial - Questions.docx", "Tutorial - Solutions (official).pdf", "solver.m"]:
            self.assertTrue((week / name).is_file(), name)
        note = week / "Example Module Week 1.md"
        text = note.read_text(encoding="utf-8")
        self.assertIn("status: files-only", text)
        self.assertIn("[[Atlas/Academic/SEM 1 Modules/TEST101 Example Module/Week 1/Lecture - Notes.pdf|", text)
        self.assertIn("[[Example Module notes|", text)
        data = json.loads(self.manifest.read_text())
        self.assertIn("lecture source.pdf", data["captured"])
        self.assertNotIn("Lecture - Notes.pdf", data["captured"])
        self.assertNotIn("Video.mp4", data["captured"])
        self.assertEqual(data["unknown"], "keep")
        self.assertEqual(data["last_sync"], dt.date.today().isoformat())
        self.notifier.assert_called_once_with(5, self.config)
        self.ingester.assert_called_once()
        note.write_text("human-owned text", encoding="utf-8")
        self.assertEqual(self.run_pull()["count"], 0)
        self.assertEqual(len(self.browser.downloaded), 5)
        self.assertEqual(note.read_text(), "human-owned text")

    def test_dry_resolves_but_only_records_run(self):
        original = self.manifest.read_bytes()
        result = self.run_pull(dry=True, ingest=True)
        self.assertEqual(result["count"], 5)
        self.assertEqual(self.browser.downloaded, [])
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertFalse((self.manifest.parent / self.module).exists())
        self.recorder.assert_called_once()
        self.notifier.assert_not_called()
        self.ingester.assert_not_called()

    def test_original_captured_name_skipped(self):
        self.data["captured"] = ["lecture source.pdf", "solver.m"]
        self.save_manifest()
        self.assertEqual(self.run_pull()["count"], 3)
        self.assertFalse(any("lecture%20source" in p for p in self.browser.downloaded))

    def test_collision_and_existing_note_never_overwritten(self):
        week = self.manifest.parent / self.module / "Week 1"
        week.mkdir(parents=True)
        original = week / "Lecture - Notes.pdf"
        original.write_bytes(b"old")
        note = week / "Example Module Week 1.md"
        note.write_text("my notes")
        self.run_pull()
        self.assertEqual(original.read_bytes(), b"old")
        self.assertTrue((week / "Lecture - Notes (2).pdf").exists())
        self.assertEqual(note.read_text(), "my notes")

    def test_partial_download_records_success_without_advancing_sync(self):
        self.browser.fail_download = "Questions.docx"
        with self.assertRaisesRegex(OSError, "synthetic"):
            self.run_pull()
        data = json.loads(self.manifest.read_text())
        self.assertEqual(data["captured"], ["lecture source.pdf"])
        self.assertEqual(data["last_sync"], "2000-01-01")
        self.recorder.assert_called_once()
        self.assertIsInstance(self.recorder.call_args.args[6], OSError)
        self.ingester.assert_not_called()

    def test_login_or_unrecognized_course_does_not_claim_success(self):
        for content, exception in [('<input type="password">', LoginRequired),
                                   ('<main id="region-main">Changed layout</main>', ValueError)]:
            with self.subTest(content=content):
                self.browser.pages["/course/view.php?id=101"] = content
                with self.assertRaises(exception):
                    self.run_pull()
                self.assertEqual(json.loads(self.manifest.read_text())["last_sync"], "2000-01-01")
                self.assertEqual(self.browser.downloaded, [])

    def test_configuration_changes_types_names_week_and_note(self):
        self.config["scan"]["take_extensions"] = [".pdf"]
        self.config["scan"]["skip_original_names"] = ["Answers.pdf"]
        self.config["paths"]["week"] = "Teaching {week}"
        self.config["paths"]["note"] = "{module} Teaching {week}.md"
        self.config["naming"]["rules"][3]["stem"] = "Reading"
        self.config["note"]["template"] = "{module} {week}:\n{links}\n"
        self.assertEqual(self.run_pull()["count"], 1)
        self.assertTrue((self.manifest.parent / self.module / "Teaching 1" / "Reading.pdf").exists())

    def test_resource_main_region_embed_and_redirect(self):
        resources = list(discover(self.browser, self.data, self.config))
        self.assertEqual(len(resources), 6)
        self.assertEqual(resources[0].original, "lecture source.pdf")
        self.assertFalse(any("setcheck" in r.url or "outside" in r.url for r in resources))
        self.assertEqual(resources[-1].week, 2)
        self.assertFalse(any("999" in url for url in self.browser.fetched))

    def test_all_embedding_types_and_missing_resource_error(self):
        url = self.browser.base + "/mod/resource/view.php?id=1"
        for tag, attr in [("iframe", "src"), ("embed", "src"), ("object", "data"), ("a", "href")]:
            self.browser.pages["/mod/resource/view.php?id=1"] = (
                f'<main role="main"><{tag} {attr}="/pluginfile.php/1/mod_resource/content/1/notes.pdf"></{tag}></main>')
            self.assertEqual(resolve(self.browser, url, "Notes", self.config)[0][2], "notes.pdf")
        self.browser.pages["/mod/resource/view.php?id=1"] = '<main role="main">Nothing</main>'
        with self.assertRaisesRegex(ValueError, "No content file"):
            resolve(self.browser, url, "Notes", self.config)

    def test_filename_headers_and_traversal(self):
        self.assertEqual(filename("https://moodle.invalid/file", {
            "content-disposition": "attachment; filename*=UTF-8''notes%20%C3%A9.pdf"}), "notes é.pdf")
        for name in ["../escape.pdf", "..\\escape.pdf", ""]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                filename("https://moodle.invalid/", {"content-disposition": f'attachment; filename="{name}"'})

    def test_guard_paths_and_remote_origin(self):
        for path in ["../outside", ".obsidian/workspace.json", ".git/config"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                conf.inside(self.vault, path, self.config)
        self.data["courses"]["101"] = "../../../../../outside"
        self.save_manifest()
        with self.assertRaises(ValueError):
            self.run_pull()
        self.assertFalse((self.root / "outside").exists())

    def test_multiple_instances_lock_releases(self):
        profile = conf.profile(self.config)
        with pull.exclusive(profile, self.config):
            with self.assertRaises(OSError), pull.exclusive(profile, self.config):
                self.fail("Lock should refuse concurrent pull")
        with pull.exclusive(profile, self.config):
            pass

    def test_card_and_anki_opt_ins(self):
        configured = list(commands(self.config, self.vault, ["Example Module"]))
        self.assertEqual(len(configured), 1)
        self.assertEqual(configured[0][1:4], ["-m", "tutor", "ingest"])
        self.config["ingest"].update(cards=True, anki_sync=True)
        self.assertEqual([c[3] for c in commands(self.config, self.vault, ["Example Module"])],
                         ["ingest", "cards", "anki-sync"])

    def test_git_handoff_and_owned_files_preserve_staged_work(self):
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.vault, capture_output=True,
                                  text=True, check=True).stdout.strip()
        git("init")
        git("config", "user.name", "Fixture User")
        git("config", "user.email", "fixture@example.invalid")
        git("add", "--", self.manifest.relative_to(self.vault).as_posix())
        git("commit", "-m", "fixture")
        unrelated = self.vault / "unrelated.txt"
        unrelated.write_text("other session")
        git("add", "--", "unrelated.txt")
        # atrium.context resolves settings at import; point it only at this temp vault.
        with patch.dict("os.environ", {"DENISSE_VAULT": str(self.vault)}):
            pull.run(self.config, self.vault, browser_factory=self.browser.factory,
                     notifier=self.notifier, ingester=self.ingester, say=lambda text: None)
            dry = pull.run(self.config, self.vault, dry=True, browser_factory=self.browser.factory,
                            notifier=self.notifier, ingester=self.ingester, say=lambda text: None)
        self.assertEqual(dry["count"], 0)
        self.assertEqual(git("diff", "--cached", "--name-only"), "unrelated.txt")
        from atrium import handoff
        text = handoff.path(vault=self.vault).read_text(encoding="utf-8")
        self.assertEqual(text.count("[keats]"), 2)
        self.assertIn("dry run", text)
        self.assertNotIn("unrelated.txt", git("show", "--format=", "--name-only", "HEAD"))

    def test_commit_failure_is_reported(self):
        self.recorder.side_effect = RuntimeError("commit failed")
        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            self.run_pull()
        self.ingester.assert_not_called()

    def test_notification_failure_is_recorded_and_not_silent(self):
        self.notifier.side_effect = RuntimeError("toast unavailable")
        with self.assertRaisesRegex(RuntimeError, "toast unavailable"):
            self.run_pull()
        self.assertIn("toast unavailable", str(self.recorder.call_args.args[6]))

    def test_notification_command_uses_configured_text_without_shell(self):
        self.config["output"]["toast"] = "New course files: {count}"
        with patch("keats.pull.subprocess.run") as run:
            pull.notify(3, self.config)
        self.assertEqual(run.call_args.args[0][-1], "New course files: 3")
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_ingest_is_detached_with_current_checkout_and_log(self):
        profile = conf.profile(self.config)
        profile.mkdir()
        with patch("keats.pull.subprocess.Popen") as popen:
            pull.start_ingest(self.vault, profile, ["Example Module"], self.config)
        self.assertEqual(popen.call_args.args[0][1:3], ["-m", "keats.ingest"])
        self.assertEqual(popen.call_args.kwargs["cwd"], Path(__file__).resolve().parents[1])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)


def benchmark():
    test = PullTests()
    test.setUp()
    try:
        start = time.perf_counter()
        result = test.run_pull(dry=True)
        print(f"Synthetic dry pull: {time.perf_counter() - start:.4f}s; "
              f"{len(test.browser.fetched)} HTML/metadata requests, {result['count']} candidate files; "
              "no browser/network/real vault; mocked handoff and git")
    finally:
        test.doCleanups()


if __name__ == "__main__":
    if "--benchmark" in sys.argv:
        benchmark()
    else:
        unittest.main()
