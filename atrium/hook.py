"""Entry point for the Claude Code hook: python <repo>/atrium/hook.py
Works from any working directory and any shell (cmd or Git Bash), because it puts
the repo on sys.path itself instead of relying on `cd`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atrium.capture import main  # noqa: E402

sys.exit(main())
