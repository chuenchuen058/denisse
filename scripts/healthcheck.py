"""
Denisse remote health check.

The first thing a remote (phone-driven) Claude session should run before it
touches anything: one command, one compact report of every moving part the
remote workflow depends on.

    .venv\\Scripts\\python.exe scripts\\healthcheck.py

Add --ask to also do a real round trip through the local model (slower, but
proves the whole stack end to end rather than just its pieces).
Exit code is 0 only if every check passes.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

results: list[tuple[bool, str, str]] = []


def check(name: str):
    """Decorator: run a check, record pass/fail plus a one-line detail."""

    def wrap(fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append((ok, name, detail))
        return fn

    return wrap


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def run_checks(do_ask: bool) -> None:
    @check("python")
    def _python():
        v = sys.version.split()[0]
        return v.startswith("3."), f"{v} at {sys.executable}"

    @check("repo")
    def _repo():
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        dirty = [ln for ln in git("status", "--porcelain").splitlines() if ln]
        head = git("log", "-1", "--format=%h %s")
        return bool(branch), f"{branch} | {len(dirty)} uncommitted | HEAD {head}"

    @check("ollama-server")
    def _ollama_server():
        import ollama

        models = [m.get("model", m.get("name", "")) for m in ollama.list().get("models", [])]
        return bool(models), f"{len(models)} model(s): {', '.join(models) or 'none'}"

    @check("model-present")
    def _model():
        import ollama

        from brain import MODEL

        models = [m.get("model", m.get("name", "")) for m in ollama.list().get("models", [])]
        return any(MODEL in m for m in models), f"want {MODEL}"

    @check("imports")
    def _imports():
        import importlib

        mods = ["brain", "tools", "safety", "memory_store", "auto_organize", "obsidian_sync"]
        bad = []
        for m in mods:
            try:
                importlib.import_module(m)
            except Exception as e:
                bad.append(f"{m}({type(e).__name__})")
        return not bad, "all clean" if not bad else "failed: " + ", ".join(bad)

    @check("tools-registry")
    def _tools():
        from tools import TOOL_FUNCTIONS

        return bool(TOOL_FUNCTIONS), f"{len(TOOL_FUNCTIONS)} tools registered"

    @check("tray-process")
    def _tray():
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/NH"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        n = out.lower().count("pythonw.exe")
        return n > 0, f"{n} pythonw.exe process(es) - tray is {'up' if n else 'DOWN'}"

    @check("conversation")
    def _conversation():
        import conversation
        import memory_store

        stored = len(memory_store.load_history())
        shown = conversation.replay()
        leaked = [m for m in shown if m.get("role") == "tool" or m.get("tool_calls")]
        return not leaked, f"{stored} stored, {len(shown)} replayed (prose only)"

    @check("voice-key")
    def _voice():
        # Presence only, never the value.
        return True, "INWORLD_API_KEY " + ("set" if os.environ.get("INWORLD_API_KEY") else "absent (local fallback)")

    if do_ask:
        @check("round-trip")
        def _roundtrip():
            from brain import Brain

            import safety

            reply, _ = Brain().respond(
                [{"role": "user", "content": "In one short sentence, tell me what model you are."}],
                confirm=lambda name, args: name not in safety.RISKY_TOOLS,
            )
            one_line = " ".join(reply.split())[:90]
            return bool(reply.strip()), one_line


def main() -> int:
    parser = argparse.ArgumentParser(description="Denisse remote health check")
    parser.add_argument("--ask", action="store_true", help="also do a live model round trip")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    run_checks(args.ask)

    width = max(len(name) for _, name, _ in results)
    for ok, name, detail in results:
        print(f"[{'OK ' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")

    failed = [name for ok, name, _ in results if not ok]
    print()
    print("all checks passed" if not failed else "FAILED: " + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
