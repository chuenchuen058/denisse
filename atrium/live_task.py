"""Entry point for the logon shortcut: pythonw <repo>/atrium/live_task.py
Puts the repo on sys.path itself (a Startup shortcut has no useful working directory),
serves the live page, and appends a failure to the vault's log (atrium/config.json
"install_live" -> "log_file"): pythonw has no console. A port already taken means the
page is already running, which is logged and is not an error."""
import datetime as dt
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atrium import conf, context, live  # noqa: E402


def _log(msg: str) -> None:
    try:
        log = context.VAULT / conf.section("install_live")["log_file"]
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def main() -> int:
    try:
        return live.serve()
    except OSError as exc:  # most often: already running on that port
        _log(f"not started: {exc}")
        return 0
    except Exception:  # noqa: BLE001
        _log(traceback.format_exc().strip())
        return 1


if __name__ == "__main__":
    sys.exit(main())
