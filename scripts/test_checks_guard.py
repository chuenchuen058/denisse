"""scripts/run_checks.py and its guard (scripts/checks_sandbox/sitecustomize.py).

    .venv\\Scripts\\python.exe scripts\\test_checks_guard.py
The guard is what keeps every other test out of Chuen's real vault and off
Ollama; if it stopped refusing, the suite would go on passing while tests
wrote to his notes. This checks it still refuses, in a child process, against
a temp folder standing in for the vault.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_checks  # noqa: E402

PROBE = r"""
import json, socket, sys
out = {}
def attempt(name, fn):
    try:
        fn(); out[name] = "ok"
    except PermissionError as e:
        out[name] = "refused" if "run_checks guard" in str(e) else f"other: {e}"
    except OSError as e:
        out[name] = f"oserror: {e}"
forbidden, allowed, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
attempt("read_forbidden", lambda: open(forbidden + "/note.md").read())
attempt("write_forbidden", lambda: open(forbidden + "/new.md", "w").write("x"))
attempt("list_forbidden", lambda: __import__("os").listdir(forbidden))
attempt("write_allowed", lambda: open(allowed + "/ok.txt", "w").write("x"))
attempt("loopback", lambda: socket.create_connection(("127.0.0.1", port), timeout=2).close())
attempt("ollama", lambda: socket.create_connection(("127.0.0.1", 11434), timeout=2).close())
print(json.dumps(out))
"""


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="denisse-guardtest-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text("private", encoding="utf-8")
        (self.tmp / "free").mkdir()
        self.log = self.tmp / "guard.log"

    def probe(self, **extra):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        env = {k: v for k, v in os.environ.items() if k != "DENISSE_CHECKS_GUARD_MODE"}
        env.update({"PYTHONPATH": str(run_checks.SANDBOX_SITE), "DENISSE_CHECKS_GUARD": str(self.log),
                    "DENISSE_CHECKS_FORBID": str(self.vault), **extra})
        try:
            run = subprocess.run([sys.executable, "-c", PROBE, str(self.vault), str(self.tmp / "free"),
                                  str(listener.getsockname()[1])],
                                 env=env, capture_output=True, text=True, timeout=60)
        finally:
            listener.close()
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout.strip().splitlines()[-1])

    def test_refuses_the_vault_and_ollama_but_not_temp_files_or_loopback(self):
        out = self.probe()
        self.assertEqual(out["read_forbidden"], "refused")
        self.assertEqual(out["write_forbidden"], "refused")
        self.assertEqual(out["list_forbidden"], "refused")
        self.assertEqual(out["ollama"], "refused")
        self.assertEqual(out["write_allowed"], "ok")
        self.assertEqual(out["loopback"], "ok")
        self.assertFalse((self.vault / "new.md").exists())
        logged = self.log.read_text(encoding="utf-8")
        self.assertIn("note.md", logged)
        self.assertIn("11434", logged)

    def test_log_mode_records_without_refusing(self):
        out = self.probe(DENISSE_CHECKS_GUARD_MODE="log")
        self.assertEqual(out["read_forbidden"], "ok")
        self.assertIn("note.md", self.log.read_text(encoding="utf-8"))

    def test_off_without_the_env_var(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("DENISSE_CHECKS_")}
        env["PYTHONPATH"] = str(run_checks.SANDBOX_SITE)
        run = subprocess.run([sys.executable, "-c", "import sys; print(len(sys.argv))"],
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)


class Runner(unittest.TestCase):
    def test_discovers_every_area_and_this_file(self):
        found = {p.relative_to(ROOT).as_posix() for p in run_checks.discover()}
        self.assertIn("scripts/test_checks_guard.py", found)
        for area in ("atrium/", "eval/", "learner/", "orchestra/", "orchestra/exams/"):
            self.assertTrue(any(f.startswith(area) for f in found), area)
        self.assertFalse(any(".venv" in f for f in found))

    def test_package_modules_run_with_dash_m(self):
        self.assertEqual(run_checks.command(ROOT / "atrium" / "test_atrium.py")[1:],
                         ["-m", "atrium.test_atrium"])
        self.assertEqual(run_checks.command(ROOT / "learner" / "test_learner.py")[1:],
                         ["-m", "learner.test_learner"])
        self.assertEqual(run_checks.command(ROOT / "scripts" / "test_tasks.py")[1:],
                         [str(ROOT / "scripts" / "test_tasks.py")])

    def test_markers(self):
        f = Path(tempfile.mkdtemp()) / "test_x.py"
        f.write_text('"""doc"""\n# checks: quarantine flips on slow disks\nx = "# checks: slow"\n',
                     encoding="utf-8")
        self.assertEqual(run_checks.markers(f), {"quarantine": "flips on slow disks"})
        self.assertIn("slow", run_checks.markers(ROOT / "orchestra" / "test_runner.py"))

    def test_forbidden_covers_the_real_vault_and_memory(self):
        names = [str(p) for p in run_checks.FORBIDDEN]
        self.assertTrue(any(n.endswith(os.path.join("OneDrive - King's College London", "Denisse"))
                            for n in names))
        self.assertIn(str(ROOT / "memory"), names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
