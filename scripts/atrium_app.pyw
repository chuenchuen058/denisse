"""Atrium hub as an app (Chuen, 12 Nov 2026: "an app for my hub so it stays live all the time").

Double-click (or a desktop shortcut to `pythonw scripts\\atrium_app.pyw`):
  1. if `python -m atrium live` isn't answering, start it hidden (it died with the
     11 Nov 18:47 restart and nothing brought it back);
  2. open the page in its own app window (Edge/Chrome --app), not a browser tab.
Host, port and the browsers to try come from atrium/config.json ("live", "app");
nothing is hardcoded here.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def config():
    cfg = json.loads((REPO / "atrium" / "config.json").read_text(encoding="utf-8"))
    return cfg["live"], cfg.get("app", {})


def url(live):
    return f"http://{live['host']}:{live['port']}/"


def answering(live, timeout):
    try:
        with urllib.request.urlopen(url(live) + "data", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start_live(app):
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe") if exe.name.lower() == "python.exe" else exe
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([str(pyw), "-m", "atrium", "live"], cwd=REPO, creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def browser(app):
    for cand in app.get("browsers", []):
        path = os.path.expandvars(cand)
        found = path if os.path.isfile(path) else shutil.which(path)
        if found:
            return found
    return None


def main():
    live, app = config()
    if not answering(live, app.get("probe_timeout_s", 2)):
        start_live(app)
        deadline = time.time() + app.get("start_wait_s", 20)
        while time.time() < deadline and not answering(live, app.get("probe_timeout_s", 2)):
            time.sleep(0.5)
    exe = browser(app)
    if exe:
        subprocess.Popen([exe, f"--app={url(live)}"] + list(app.get("browser_args", [])))
    else:
        os.startfile(url(live))


if __name__ == "__main__":
    main()
