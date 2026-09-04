"""
One-shot headless runner for Denisse.

Made for driving Denisse from a phone: a remote Claude session runs this
with a single command and reads the reply off stdout, instead of trying to
hold an interactive `main.py` REPL open across tool calls.

    .venv\\Scripts\\python.exe scripts\\ask.py "what time is it"

Flags:
    --history       use and persist memory/conversation.json (default: the
                    run is ephemeral, so remote smoke tests don't pollute
                    Denisse's real conversation memory)
    --allow-risky   auto-approve tools in safety.RISKY_TOOLS. Off by default:
                    nobody is at the keyboard to answer a confirmation prompt,
                    so a gated tool is refused rather than silently run.
    --context TEXT  extra context string passed to Brain.respond
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import conversation  # noqa: E402
import safety  # noqa: E402


def build_confirm(allow_risky: bool):
    """Non-interactive stand-in for safety.confirm_risky_tool."""

    def confirm(name: str, args: dict) -> bool:
        if name not in safety.RISKY_TOOLS:
            return True
        if allow_risky:
            print(f"[allowed risky tool: {name}]", file=sys.stderr)
            return True
        print(f"[refused risky tool: {name} - rerun with --allow-risky]", file=sys.stderr)
        return False

    return confirm


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask Denisse one question, print one reply.")
    parser.add_argument("prompt", nargs="+", help="what to ask")
    parser.add_argument("--history", action="store_true", help="use and persist conversation.json")
    parser.add_argument("--allow-risky", action="store_true", help="auto-approve gated tools")
    parser.add_argument("--context", default="", help="extra context for Brain.respond")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    try:
        brain = conversation.get_brain()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    messages = conversation.replay() if args.history else []
    turn_start = len(messages)
    messages.append({"role": "user", "content": " ".join(args.prompt)})

    try:
        reply, messages = brain.respond(
            messages,
            confirm=build_confirm(args.allow_risky),
            extra_context=args.context,
        )
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.history:
        conversation.record(messages[turn_start:])

    print(reply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
