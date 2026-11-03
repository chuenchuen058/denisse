"""Run the whole offline test suite: every test_*.py in the repo, in parallel.

    .venv\\Scripts\\python.exe scripts\\run_checks.py           # everything
    .venv\\Scripts\\python.exe scripts\\run_checks.py --fast    # skip "slow" modules
    .venv\\Scripts\\python.exe scripts\\run_checks.py invest    # only paths containing "invest"

No Ollama, microphone, network or API keys are used. Each module runs in its
own process with a temp vault (DENISSE_VAULT) and rules file, and
scripts/checks_sandbox/sitecustomize.py refuses -- and fails the module for --
any access to Chuen's real vault or memory folders and any network connection
(Ollama included). A test that needs one of those must skip with a reason.

Markers, put on a line of their own in a test module:

    # checks: slow                      skipped by --fast
    # checks: quarantine <reason>       never run; listed with its reason

Exit status is non-zero if any module fails, times out or tries the guard.
Real-model evals (eval/run_eval.py) and the healthcheck stay separate.
"""
import argparse
from html import escape
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
SANDBOX_SITE = ROOT / "scripts" / "checks_sandbox"
TIMEOUT = 150  # seconds per module; the slowest takes ~30s alone
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "models", "memory"}
MARKER = re.compile(r"^#\s*checks:\s*(slow|quarantine)\b[ \t]*(.*)$", re.M)
HOME = Path(os.path.expanduser("~"))
# Never touched by a test: the real vault, Claude's memory, and the tray's
# memory folders (this checkout's, and the main checkout's when this is a
# worktree of it).
FORBIDDEN = [HOME / "OneDrive - King's College London" / "Denisse",
             HOME / ".claude", ROOT / "memory", HOME / "GitHub" / "denisse" / "memory"]


def discover(root=ROOT):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        found += [Path(dirpath) / f for f in sorted(filenames)
                  if f.startswith("test_") and f.endswith(".py")]
    return found


def command(path):
    """Modules inside a top-level package (atrium, learner) import their
    package by name, so they run with -m from the repo root; the rest are
    scripts (orchestra/ has no __init__.py, so orchestra/exams is too)."""
    parts = [path.stem]
    d = path.parent
    while (d / "__init__.py").exists() and d != ROOT:
        parts.insert(0, d.name)
        d = d.parent
    if len(parts) > 1 and d == ROOT:
        return [sys.executable, "-m", ".".join(parts)]
    return [sys.executable, str(path)]


def markers(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    return {kind: reason.strip() for kind, reason in MARKER.findall(text)}


def summary_line(out):
    """The module's own verdict: unittest's last line or a check script's tally."""
    ran = re.findall(r"^Ran (\d+) tests?", out, re.M)
    verdict = re.findall(r"^(OK|FAILED|NO TESTS RAN)\b.*$", out, re.M)
    if ran:
        line = f"{ran[-1]} tests"
        tail = re.findall(r"^(?:OK|FAILED|NO TESTS RAN)\b(.*)$", out, re.M)
        extra = tail[-1].strip() if tail else ""
        return f"{line} {verdict[-1] if verdict else ''} {extra}".strip()
    tally = re.findall(r"^.*\b\d+/\d+\b.*(?:passed|ok|OK).*$", out, re.M)
    if tally:
        return tally[-1].strip()[:60]
    lines = [l for l in out.strip().splitlines() if l.strip()]
    return lines[-1].strip()[:60] if lines else ""


def test_count(out):
    """Read unittest's count or the custom scripts' passed/total tally."""
    ran = re.findall(r"^Ran (\d+) tests?\b", out, re.M)
    if ran:
        return int(ran[-1])
    tally = re.findall(r"^.*\b\d+/(\d+)\b.*(?:passed|ok|OK).*$", out, re.M)
    return int(tally[-1]) if tally else None


def github_summary(rows):
    """Append a failures-first summary when running inside GitHub Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    ordered = sorted(rows, key=lambda r: (r["passed"], r["suite"]))
    lines = ["", "## Offline regression checks", "",
             "| Module | Result | Seconds | Test count |",
             "| --- | --- | ---: | ---: |"]
    for row in ordered:
        suite = escape(row["suite"]).replace("|", "&#124;")
        count = test_count(row.get("output", ""))
        lines.append(f"| <code>{suite}</code> | {row['status']} | {row['seconds']:.1f} | "
                     f"{count if count is not None else '-'} |")
    lines += ["", "Test count is `-` when the module did not report a count (including skipped modules).", ""]
    for row in ordered:
        if row["passed"]:
            continue
        excerpt = "\n".join(row.get("output", "").splitlines()[:20])
        lines += ["<details>", f"<summary>{escape(row['suite'])} — {row['status']} (first 20 lines)</summary>",
                  "", f"<pre>{escape(excerpt or '(no output captured)')}</pre>", "", "</details>", ""]
    with Path(path).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


def run_one(path, env, guard_dir):
    rel = path.relative_to(ROOT).as_posix()
    log = guard_dir / (rel.replace("/", "__") + ".guard")
    started = time.monotonic()
    try:
        run = subprocess.run(command(path), cwd=ROOT, env={**env, "DENISSE_CHECKS_GUARD": str(log)},
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=TIMEOUT,
                             stdin=subprocess.DEVNULL)
        out, code = (run.stdout or "") + (run.stderr or ""), run.returncode
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")) \
            + f"\n[run_checks] killed after {TIMEOUT}s\n"
        code = 124
    seconds = round(time.monotonic() - started, 1)
    refusals = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    if code == 124:
        status = "TIMEOUT"
    elif refusals:
        status = "GUARD"
    else:
        status = "PASS" if code == 0 else "FAIL"
    detail = summary_line(out)
    if refusals:
        detail = f"{len(refusals)} refused: {refusals[0][:70]}"
    return {"suite": rel, "status": status, "passed": status == "PASS", "exit_code": code,
            "seconds": seconds, "detail": detail, "output": out, "refusals": refusals}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("only", nargs="*", help="run only modules whose path contains one of these")
    parser.add_argument("--fast", action="store_true", help="skip modules marked '# checks: slow'")
    parser.add_argument("--jobs", "-j", type=int, default=min(8, os.cpu_count() or 2))
    parser.add_argument("--verbose", "-v", action="store_true", help="print every module's output")
    parser.add_argument("--report", type=Path, help="optional JSON summary, without transcripts")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("these suites import the Windows desktop modules; use Python 3.12 on Windows")

    rows, todo = [], []
    for path in discover():
        rel = path.relative_to(ROOT).as_posix()
        if args.only and not any(o.replace("\\", "/") in rel for o in args.only):
            continue
        mark = markers(path)
        if "quarantine" in mark:
            rows.append({"suite": rel, "status": "QUARANTINED", "passed": True, "exit_code": None,
                         "seconds": 0.0, "detail": mark["quarantine"] or "(no reason given)"})
        elif args.fast and "slow" in mark:
            rows.append({"suite": rel, "status": "SKIP-SLOW", "passed": True, "exit_code": None,
                         "seconds": 0.0, "detail": "marked slow; run without --fast"})
        else:
            todo.append(path)
    if not todo:
        print("run_checks: no test modules selected -- that is not a pass", flush=True)
        return 1

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="denisse-checks-") as tmp:
        sandbox = Path(tmp)
        (sandbox / "vault").mkdir()
        (sandbox / "guard").mkdir()
        rules = sandbox / "rules.json"
        rules.write_text("{}", encoding="utf-8")
        pythonpath = os.pathsep.join([str(SANDBOX_SITE)] + (
            [os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []))
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": pythonpath,
               "DENISSE_CHECKS_FORBID": os.pathsep.join(str(p) for p in FORBIDDEN),
               "DENISSE_VAULT": str(sandbox / "vault"), "DENISSE_RULES_FILE": str(rules),
               "DENISSE_REPLY_PREFERENCES": str(sandbox / "reply_preferences.json"),
               "DENISSE_TOOL_ROUTER": "1", "DENISSE_SKILLS_CORE": "1",
               "DENISSE_INSTINCTS": "1", "DENISSE_VAULT_SEARCH": "1",
               "DENISSE_STYLE_EXAMPLES": "1", "INWORLD_API_KEY": "",
               "SPOTIFY_CLIENT_ID": "", "SPOTIFY_CLIENT_SECRET": "",
               "OLLAMA_HOST": "http://127.0.0.1:11434"}
        env.pop("DENISSE_CHECKS_GUARD_MODE", None)
        with ThreadPoolExecutor(max(1, args.jobs)) as pool:
            futures = [pool.submit(run_one, p, env, sandbox / "guard") for p in todo]
            for f in futures:
                r = f.result()
                rows.append(r)
                if args.verbose or not r["passed"]:
                    print(f"\n--- {r['suite']} ({r['status']}, exit {r['exit_code']}) ---", flush=True)
                    print(r["output"].rstrip()[-6000:], flush=True)
                    for line in r["refusals"]:
                        print(f"[guard] {line}", flush=True)
    wall = round(time.monotonic() - started, 1)

    rows.sort(key=lambda r: r["suite"])
    width = max(len(r["suite"]) for r in rows)
    print(f"\n{'module':<{width}}  {'result':<11} {'secs':>6}  detail")
    print(f"{'-' * width}  {'-' * 11} {'-' * 6}  {'-' * 30}")
    for r in rows:
        print(f"{r['suite']:<{width}}  {r['status']:<11} {r['seconds']:>6.1f}  {r['detail']}")
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    failed = [r for r in rows if not r["passed"]]
    print(f"\n{len(rows)} modules: " + ", ".join(f"{n} {k.lower()}" for k, n in sorted(counts.items()))
          + f" -- {wall}s wall, {args.jobs} at a time", flush=True)

    github_summary(rows)
    if args.report:
        report = {"kind": "offline-regressions", "python": sys.version.split()[0],
                  "platform": sys.platform, "fast": args.fast, "wall_seconds": wall,
                  "passed_suites": sum(r["status"] == "PASS" for r in rows),
                  "total_suites": len(rows),
                  "results": [{k: v for k, v in r.items() if k not in ("output",)} for r in rows]}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
