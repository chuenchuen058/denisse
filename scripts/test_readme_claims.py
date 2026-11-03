"""README claims stay true: links resolve and the results table matches its JSON.

README.md is what a stranger reads first, so it must not drift from the repo.

  * every relative link (Markdown `[..](..)` and HTML `src=`/`href=`) points
    at a file or folder that exists in this checkout;
  * the "Measured results" section cites one file in demo/results/, that file
    is the latest one there, and every number in its table equals the value
    at the field named in the table header for that row's eval.

Table convention (README "Measured results"): the first column names the
eval in backticks, e.g. `dialogue` or `learning.learning_on`, which resolves
to parts.dialogue.summary or parts.learning.summary.learning_on. Every other
header names its field(s) in backticks, e.g. (`passes` / `attempts`), and
the cell holds that many numbers in the same order.

    .venv\\Scripts\\python.exe -m unittest discover -s scripts -p "test_readme_claims.py" -v
"""
import json
from pathlib import Path
import re
import unittest
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
RESULTS = ROOT / "demo" / "results"

FENCE = re.compile(r"^```.*?^```", re.M | re.S)
MD_LINK = re.compile(r"(?<!\\)\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HTML_LINK = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']+)["']""", re.I)
TICKED = re.compile(r"`([^`]+)`")
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _text() -> str:
    return README.read_text(encoding="utf-8")


def relative_links(text: str) -> list[str]:
    body = FENCE.sub("", text)
    targets = MD_LINK.findall(body) + HTML_LINK.findall(body)
    out = []
    for target in targets:
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith(("#", "//")):
            continue  # http(s), mailto, in-page anchors
        out.append(target)
    return out


def section(text: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        raise AssertionError(f"README has no '## {heading}' section")
    return match.group(1)


def table_rows(block: str) -> list[list[str]]:
    rows = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue  # the |---| separator
        rows.append(cells)
    return rows


def lookup(data, dotted: str):
    for key in dotted.split("."):
        if not isinstance(data, dict) or key not in data:
            raise KeyError(dotted)
        data = data[key]
    return data


class ReadmeLinks(unittest.TestCase):
    def test_relative_links_resolve(self):
        links = relative_links(_text())
        self.assertGreater(len(links), 5, "expected README to link its evidence")
        missing = []
        for link in links:
            path = unquote(link.split("#", 1)[0].split("?", 1)[0])
            if not path:
                continue
            if not (ROOT / path).resolve().exists():
                missing.append(link)
        self.assertEqual(missing, [], f"README links that do not resolve: {missing}")

    def test_link_parser_sees_both_kinds(self):
        sample = '[a](docs/x.md) <img src="assets/y.svg"/> [b](https://e.com) [c](#top)\n```\n[d](no.md)\n```'
        self.assertEqual(sorted(relative_links(sample)), ["assets/y.svg", "docs/x.md"])

    def test_ci_badge_present(self):
        self.assertIn("actions/workflows/checks.yml/badge.svg", _text())
        self.assertTrue((ROOT / ".github" / "workflows" / "checks.yml").exists())


class ReadmeResults(unittest.TestCase):
    def setUp(self):
        self.block = section(_text(), "Measured results")
        cited = sorted(set(re.findall(r"\]\((demo/results/[^)#\s]+\.json)\)", self.block)))
        self.assertEqual(len(cited), 1, f"Measured results must cite exactly one results file, found {cited}")
        self.cited = ROOT / cited[0]
        self.data = json.loads(self.cited.read_text(encoding="utf-8"))

    def test_cites_latest_results(self):
        latest = sorted(RESULTS.glob("*.json"))[-1]
        self.assertEqual(self.cited.name, latest.name,
                         f"README cites {self.cited.name} but the latest results file is {latest.name}")

    def test_table_numbers_equal_cited_json(self):
        rows = table_rows(self.block)
        self.assertGreaterEqual(len(rows), 2, "Measured results has no table")
        header, body = rows[0], rows[1:]
        fields = [TICKED.findall(h) for h in header]
        self.assertTrue(all(fields[1:]), f"every results column must name its field(s): {header}")
        checked = 0
        for row in body:
            self.assertEqual(len(row), len(header), row)
            names = TICKED.findall(row[0])
            self.assertEqual(len(names), 1, f"first cell must name one eval in backticks: {row[0]}")
            part, _, sub = names[0].partition(".")
            base = f"parts.{part}.summary" + (f".{sub}" if sub else "")
            for cell, keys in zip(row[1:], fields[1:]):
                numbers = NUMBER.findall(cell)
                self.assertEqual(len(numbers), len(keys),
                                 f"{names[0]}: cell {cell!r} should hold {len(keys)} number(s) for {keys}")
                for shown, key in zip(numbers, keys):
                    path = f"{base}.{key}"
                    actual = lookup(self.data, path)
                    self.assertIsInstance(actual, (int, float), path)
                    self.assertEqual(float(shown), float(actual),
                                     f"README shows {shown} for {path}; {self.cited.name} has {actual}")
                    self.assertEqual(shown, str(actual),
                                     f"README shows {shown!r} for {path}; write it as {actual!r}")
                    checked += 1
        self.assertGreater(checked, 0)

    def test_cited_parts_ran(self):
        for row in table_rows(self.block)[1:]:
            part = TICKED.findall(row[0])[0].split(".")[0]
            self.assertEqual(self.data["parts"][part]["status"], "ran", part)


if __name__ == "__main__":
    unittest.main()
