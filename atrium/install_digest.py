r"""Register the hourly laptop digest as a Windows scheduled task (name, interval, python
and script: atrium/config.json "install_digest"; shipped: "Denisse Atrium Digest", hourly,
the venv's pythonw.exe running atrium/digest_task.py).

    .venv\Scripts\python.exe atrium\install_digest.py              # print the command only
    .venv\Scripts\python.exe atrium\install_digest.py --register   # run it (/F: replaces, so safe twice)
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atrium import conf, digest  # noqa: E402


def command(repo: Path | None = None) -> list[str]:
    c = conf.section("install_digest")
    repo = Path(repo) if repo else digest.repo_root()
    kw = {"name": c["task_name"], "python": str(repo / c["python"]), "script": str(repo / c["script"])}
    return [a.format(**kw) for a in c["create"]]


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = command()
    print(subprocess.list2cmdline(cmd))
    if "--register" not in argv:
        return 0
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
