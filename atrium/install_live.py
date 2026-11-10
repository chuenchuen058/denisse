r"""Start the live Atrium page hidden at logon, and add shortcuts that open it
(atrium/config.json "install_live"; shipped: a Startup-folder shortcut running the venv's
pythonw.exe on atrium/live_task.py, plus "Atrium Live" on the desktop and in the Start menu).
schtasks ONLOGON needs admin on this laptop, so the per-user Startup folder is used.

    .venv\Scripts\python.exe atrium\install_live.py              # print the PowerShell only
    .venv\Scripts\python.exe atrium\install_live.py --register   # run it (overwrites, so safe twice)
"""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atrium import conf, digest, live  # noqa: E402


def _q(s: str) -> str:
    """A PowerShell single-quoted string."""
    return "'" + str(s).replace("'", "''") + "'"


def browser_target(browser: str | None = None) -> str | None:
    c = conf.section("install_live")
    target = browser or c.get("browser")
    if not target:
        return None
    if "%" in target:
        return f"[System.Environment]::ExpandEnvironmentVariables({_q(target)})"
    return _q(target)


def script(repo: Path | None = None, browser: str | None = None) -> str:
    c = conf.section("install_live")
    repo = Path(repo) if repo else digest.repo_root()
    python, task = repo / c["python"], repo / c["script"]
    browser_exe = browser_target(browser) if browser is not None else None
    lines = ["$sh = New-Object -ComObject WScript.Shell"]
    for s in c["shortcuts"]:
        dest = f"(Join-Path ([Environment]::GetFolderPath({_q(s['folder'])})) {_q(s['name'])})"
        if s.get("run"):
            lines += [f"$l = $sh.CreateShortcut({dest})",
                      f"$l.TargetPath = {_q(str(python))}",
                      f"$l.Arguments = {_q(chr(34) + str(task) + chr(34))}",
                      f"$l.WorkingDirectory = {_q(repo)}",
                      "$l.WindowStyle = 7",
                      "$l.Save()"]
        elif browser_exe is not None:
            lines += [f"$l = $sh.CreateShortcut({dest})",
                      f"$l.TargetPath = {browser_exe}",
                      f"$l.Arguments = {_q(live.url())}",
                      f"$l.WorkingDirectory = {_q(repo)}",
                      "$l.WindowStyle = 7",
                      "$l.Save()"]
        else:
            body = "[InternetShortcut]`r`nURL=" + live.url()
            lines.append(f"Set-Content -LiteralPath {dest} -Value \"{body}\" -Encoding ASCII")
        lines.append(f"Write-Output ('wrote ' + {dest})")
    return "; ".join(lines)


def command(repo: Path | None = None, browser: str | None = None) -> list[str]:
    return [*conf.section("install_live")["powershell"], script(repo, browser)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--browser")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    cmd = command(browser=args.browser)
    print(subprocess.list2cmdline(cmd))
    if not args.register:
        return 0
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
