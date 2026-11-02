"""Docs check: every script the operator's guide tells Chuen to run must exist.

Reads the ```commands``` blocks of docs/operator_guide.md and, for each line, checks
  * any path ending in .py / .ps1 / .mjs (e.g. orchestra\\status.py) is a file in the repo;
  * any `python -m pkg.mod` names a module file or package in the repo.
PowerShell cmdlets (Disable-ScheduledTask ...) carry no path and are skipped.

    .venv\\Scripts\\python.exe -m unittest orchestra.test_docs -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / "docs" / "operator_guide.md"

BLOCK = re.compile(r"^```commands[ \t]*\n(.*?)^```", re.S | re.M)
SCRIPT = re.compile(r"(?<![\w.\\/-])((?:[\w.-]+[\\/])*[\w.-]+\.(?:py|ps1|mjs))\b")
MODULE = re.compile(r"\s-m\s+([\w.]+)")
VENV_PY = re.compile(r"^\.venv[\\/]Scripts[\\/]python\.exe$", re.I)


def command_lines(text: str) -> list[str]:
    return [ln.strip() for block in BLOCK.findall(text) for ln in block.splitlines() if ln.strip()]


def script_paths(line: str) -> list[str]:
    return [p for p in SCRIPT.findall(line) if not VENV_PY.match(p)]


def module_exists(mod: str) -> bool:
    base = REPO.joinpath(*mod.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


class OperatorGuideCommands(unittest.TestCase):
    def setUp(self):
        self.assertTrue(GUIDE.is_file(), f"missing {GUIDE}")
        self.lines = command_lines(GUIDE.read_text(encoding="utf-8"))

    def test_guide_has_command_blocks(self):
        # Guards against the check passing vacuously because the fence name changed.
        self.assertGreaterEqual(len(self.lines), 15, "too few ```commands``` lines found")

    def test_every_script_path_exists(self):
        checked = 0
        for line in self.lines:
            for p in script_paths(line):
                checked += 1
                with self.subTest(line=line, path=p):
                    self.assertTrue((REPO / p.replace("\\", "/")).is_file(), f"{p} does not exist")
        self.assertGreaterEqual(checked, 8)

    def test_every_module_exists(self):
        checked = 0
        for line in self.lines:
            for mod in MODULE.findall(line):
                checked += 1
                with self.subTest(line=line, module=mod):
                    self.assertTrue(module_exists(mod), f"python -m {mod}: no such module in the repo")
        self.assertGreaterEqual(checked, 5)

    def test_venv_python_is_the_interpreter(self):
        for line in self.lines:
            if "python" in line.lower() and not line.lower().startswith("powershell"):
                with self.subTest(line=line):
                    self.assertTrue(line.startswith(".venv\\Scripts\\python.exe "),
                                    "use the venv python, from the repo root")


class Parsing(unittest.TestCase):
    def test_extracts_paths_and_modules(self):
        text = ("x\n```commands\n.venv\\Scripts\\python.exe orchestra\\status.py --json\n"
                ".venv\\Scripts\\python.exe -m atrium snapshot\n"
                "powershell -File orchestra\\install_task.ps1\n```\n```\nnot\\checked.py\n```\n")
        lines = command_lines(text)
        self.assertEqual(len(lines), 3)
        self.assertEqual(script_paths(lines[0]), ["orchestra\\status.py"])
        self.assertEqual(MODULE.findall(lines[1]), ["atrium"])
        self.assertEqual(script_paths(lines[2]), ["orchestra\\install_task.ps1"])

    def test_missing_path_is_caught(self):
        self.assertFalse((REPO / "orchestra/campaign_does_not_exist.py").is_file())
        self.assertFalse(module_exists("orchestra.no_such_module"))


if __name__ == "__main__":
    unittest.main()
