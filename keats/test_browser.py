"""Browser protocol fakes: never starts Chrome or contacts a server."""
from contextlib import nullcontext
import copy
import datetime as dt
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from keats import conf
from keats.browser import Chrome, session
from keats.install import main as install, task_xml
from keats.scan import LoginRequired
from keats.__main__ import main


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = conf.load()
        self.config["paths"]["profile"] = self.tmp.name
        self.config["browser"]["base_url"] = "https://moodle.invalid/"
        self.page = Mock()
        self.page.url = "https://moodle.invalid/my/"
        self.page.content.return_value = '<a href="/login/logout.php">Log out</a>'
        self.context = Mock(pages=[self.page])
        self.playwright = Mock()
        self.playwright.chromium.launch_persistent_context.return_value = self.context
        api = types.ModuleType("playwright.sync_api")
        api.sync_playwright = lambda: nullcontext(self.playwright)
        self.modules = patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.sync_api": api})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_first_pull_visible_then_headless_and_explicit_login_visible(self):
        for login, headless in [(False, False), (False, True), (True, False)]:
            with session(self.config, login=login, say=lambda s: None):
                pass
            kwargs = self.playwright.chromium.launch_persistent_context.call_args.kwargs
            self.assertEqual(kwargs["channel"], "chrome")
            self.assertEqual(kwargs["headless"], headless)
        self.assertEqual(self.context.close.call_count, 3)

    def test_unattended_never_opens_login_window(self):
        with self.assertRaises(LoginRequired), session(self.config, unattended=True):
            self.fail("must not yield")
        self.playwright.chromium.launch_persistent_context.assert_not_called()

    def test_expired_session_gives_action_and_closes_context(self):
        (Path(self.tmp.name) / self.config["paths"]["ready"]).touch()
        self.page.url = "https://moodle.invalid/login/index.php"
        self.page.content.return_value = '<input type="password">'
        with self.assertRaisesRegex(LoginRequired, "python -m keats login"), session(self.config):
            self.fail("must not yield")
        self.context.close.assert_called_once()

    def test_login_waits_for_positive_authenticated_marker(self):
        self.page.content.side_effect = ['<main>SSO</main>', '<a href="/login/logout.php">Log out</a>']
        with session(self.config, login=True, say=lambda s: None):
            pass
        self.page.wait_for_timeout.assert_called_once_with(self.config["browser"]["login_poll_ms"])

    def test_fetch_and_download_fake_page(self):
        browser = Chrome(self.context, self.config)
        self.page.evaluate.return_value = {"url": "https://moodle.invalid/course/view.php?id=1",
                                           "headers": {"content-type": "text/html"}, "html": "test"}
        self.assertEqual(browser.fetch("https://moodle.invalid/course/view.php?id=1").html, "test")
        download = Mock()
        self.page.expect_download.return_value = nullcontext(types.SimpleNamespace(value=download))
        target = Path(self.tmp.name) / "notes.pdf"
        browser.download("https://moodle.invalid/pluginfile.php/1/mod_resource/content/1/notes.pdf?a=b", target)
        self.assertIn("forcedownload=1", self.page.evaluate.call_args.args[1])
        download.save_as.assert_called_once_with(target)

    def test_fetch_cors_expiry_becomes_login_message(self):
        browser = Chrome(self.context, self.config)
        self.page.evaluate.side_effect = RuntimeError("Failed to fetch")
        self.page.url = "https://login.microsoftonline.com/tenant"
        self.page.content.return_value = '<input type="password">'
        with self.assertRaises(LoginRequired):
            browser.fetch("https://moodle.invalid/course/view.php?id=1")

    def test_cli_returns_nonzero_with_one_login_line(self):
        output = io.StringIO()
        with patch("keats.__main__.conf.load", return_value=self.config), \
             patch("keats.__main__.conf.vault", return_value=Path(self.tmp.name)), \
             patch("keats.__main__.run", side_effect=LoginRequired(self.config["output"]["login_required"])), \
             patch("sys.stderr", output):
            self.assertEqual(main(["pull"]), 1)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("python -m keats login", output.getvalue())

    def test_schedule_xml_ac_online_daily_and_paths(self):
        self.config["schedule"]["time"] = "04:23"
        repo = Path(self.tmp.name) / "repo with spaces & symbols"
        xml = task_xml(self.config, repo, dt.datetime(2026, 1, 2, 10), "TEST\\user")
        root = ET.fromstring(xml)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        def value(path):
            return root.find(path, ns).text
        self.assertEqual(value("t:Triggers/t:CalendarTrigger/t:StartBoundary"), "2026-01-03T04:23:00")
        self.assertEqual(value("t:Triggers/t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval"), "1")
        for name in ["DisallowStartIfOnBatteries", "StopIfGoingOnBatteries", "RunOnlyIfNetworkAvailable"]:
            self.assertEqual(value("t:Settings/t:" + name), "true")
        self.assertEqual(value("t:Principals/t:Principal/t:LogonType"), "InteractiveToken")
        self.assertEqual(value("t:Actions/t:Exec/t:WorkingDirectory"), str(repo.resolve()))
        self.assertIn("repo with spaces & symbols", value("t:Actions/t:Exec/t:Arguments"))

    def test_installer_preview_does_not_register(self):
        with patch("keats.install.subprocess.run") as run, patch("sys.stdout", io.StringIO()):
            self.assertEqual(install([]), 0)
        run.assert_not_called()

    def test_registration_requires_flag_and_uses_xml_argument(self):
        def register(command, **kwargs):
            self.assertEqual(command[:2], ["schtasks", "/Create"])
            xml = Path(command[command.index("/XML") + 1]).read_text(encoding="utf-16")
            ET.fromstring(xml)
            return types.SimpleNamespace(returncode=0)
        with patch("keats.install.subprocess.run", side_effect=register) as run, patch("sys.stdout", io.StringIO()):
            self.assertEqual(install(["--register"]), 0)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
