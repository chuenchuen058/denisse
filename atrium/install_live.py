r"""Start the live Atrium page hidden at logon, and add shortcuts that open it
(atrium/config.json "install_live"; shipped: a Startup-folder shortcut running the venv's
pythonw.exe on atrium/live_task.py, plus "Atrium Live" on the desktop and in the Start menu).
schtasks ONLOGON needs admin on this laptop, so the per-user Startup folder is used.

    .venv\Scripts\python.exe atrium\install_live.py              # print the PowerShell only
    .venv\Scripts\python.exe atrium\install_live.py --register   # run it (overwrites, so safe twice)
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atrium import conf, digest, live  # noqa: E402


def _q(s: str) -> str:
    """A PowerShell single-quoted string."""
    return "'" + str(s).replace("'", "''") + "'"


def script(repo: Path | None = None) -> str:
    c = conf.section("install_live")
    repo = Path(repo) if repo else digest.repo_root()
    python, task = repo / c["python"], repo / c["script"]
    lines = ["$sh = New-Object -ComObject WScript.Shell"]
    for s in c["shortcuts"]:
        dest = f"(Join-Path ([Environment]::GetFolderPath({_q(s['folder'])})) {_q(s['name'])})"
        if s.get("run"):
            lines += [f"$l = $sh.CreateShortcut({dest})", f"$l.TargetPath = {_q(python)}",
                      f"$l.Arguments = {_q(chr(34) + str(task) + chr(34))}", f"$l.WorkingDirectory = {_q(repo)}",
                      "$l.WindowStyle = 7", "$l.Save()"]
        else:
            body = "[InternetShortcut]`r`nURL=" + live.url()
            lines.append(f"Set-Content -LiteralPath {dest} -Value \"{body}\" -Encoding ASCII")
        lines.append(f"Write-Output ('wrote ' + {dest})")
    return "; ".join(lines)


def command(repo: Path | None = None) -> list[str]:
    return [*conf.section("install_live")["powershell"], script(repo)]


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = command()
    print(subprocess.list2cmdline(cmd))
    if "--register" not in argv:
        return 0
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
