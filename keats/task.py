"""Scheduler entry: no window or interactive login; retain output for pythonw."""
from contextlib import redirect_stderr, redirect_stdout
import datetime as dt
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from keats import conf  # noqa: E402
from keats.__main__ import main as cli  # noqa: E402


def main():
    config = conf.load()
    profile = conf.profile(config)
    profile.mkdir(parents=True, exist_ok=True)
    with (profile / config["schedule"]["task_log"]).open("a", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            print(dt.datetime.now().isoformat(timespec="seconds"))
            return cli(["pull", "--unattended"])


if __name__ == "__main__":
    raise SystemExit(main())
