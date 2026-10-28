"""Run isolated offline regression suites; no Ollama, microphone or API keys.

The desktop and native audio checks remain separate, as do real-model evals.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    "scripts/test_brain_guards.py",
    "eval/test_router.py",
    "eval/test_learning.py",
    "eval/test_self_correct.py",
    "scripts/test_study_command.py",
    "scripts/test_dialogue_reliability.py",
    "scripts/test_adaptive_brain.py",
    "scripts/test_pet_chat.py",
    "scripts/test_vault_git.py",
    "scripts/test_standing_instructions.py",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="optional JSON summary, without transcripts")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("these suites import the Windows desktop modules; use Python 3.12 on Windows")
    results = []
    with tempfile.TemporaryDirectory(prefix="denisse-checks-") as tmp:
        sandbox = Path(tmp)
        (sandbox / "vault").mkdir()
        rules = sandbox / "rules.json"
        rules.write_text("{}", encoding="utf-8")
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
               "DENISSE_VAULT": str(sandbox / "vault"), "DENISSE_RULES_FILE": str(rules),
               "DENISSE_TOOL_ROUTER": "1", "DENISSE_SKILLS_CORE": "1",
               "DENISSE_INSTINCTS": "1", "DENISSE_VAULT_SEARCH": "1",
               "DENISSE_STYLE_EXAMPLES": "1", "INWORLD_API_KEY": "",
               "SPOTIFY_CLIENT_ID": "", "SPOTIFY_CLIENT_SECRET": ""}
        for suite in SUITES:
            started = time.monotonic()
            print(f"\n--- {suite} ---", flush=True)
            try:
                run = subprocess.run([sys.executable, str(ROOT / suite)], cwd=ROOT, env=env,
                                     capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", timeout=120)
                print(run.stdout, end="", flush=True)
                print(run.stderr, end="", flush=True)
                code = run.returncode
            except subprocess.TimeoutExpired:
                print("Suite exceeded its 120-second limit", flush=True)
                code = 124
            results.append({"suite": suite, "passed": code == 0, "exit_code": code,
                            "seconds": round(time.monotonic() - started, 3)})
    report = {"kind": "offline-regressions", "python": sys.version.split()[0],
              "platform": sys.platform, "passed_suites": sum(r["passed"] for r in results),
              "total_suites": len(results), "results": results}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nOffline checks: {report['passed_suites']}/{len(results)} suites passed", flush=True)
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
