"""Entry point for the hourly scheduled task: pythonw <repo>/atrium/digest_task.py
Puts the repo on sys.path itself (a scheduled task has no useful working directory),
runs the digest, and appends the outcome -- or the error -- to the vault's digest log
(atrium/config.json "install_digest" -> "log_file"): pythonw has no console to print to."""
import datetime as dt
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atrium import conf, digest, handoff  # noqa: E402


def main(argv=None) -> int:
    try:
        msg, code = digest.run(), 0
    except Exception:  # noqa: BLE001 -- logged; the next hour tries again
        msg, code = traceback.format_exc().strip(), 1
    try:
        log = handoff.VAULT / conf.section("install_digest")["log_file"]
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass
    print(msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
