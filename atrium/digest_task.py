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
import no_window  # noqa: E402  pythonw: the hourly run opens no console window for its children

no_window.install()


def game_mode() -> str | None:
    """Why not to run now: Chuen is playing (orchestra/gamemode.py); the next hour after it runs."""
    try:
        from orchestra import gamemode
        return gamemode.CFG["tick_result"] if gamemode.is_on() else None
    except Exception:  # noqa: BLE001 -- unknown: run as usual
        return None


def main(argv=None) -> int:
    why = game_mode()
    if why:
        print(f"skipped: {why}")
        return 0
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
