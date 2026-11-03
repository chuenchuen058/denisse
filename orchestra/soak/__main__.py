"""`python -m orchestra.soak`: see harness.main for the arguments."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402  (top-level import: the same module object the tests use)

if __name__ == "__main__":
    raise SystemExit(harness.main(sys.argv[1:]))
