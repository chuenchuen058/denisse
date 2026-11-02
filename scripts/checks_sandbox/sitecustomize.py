"""Loaded into every Python process that scripts/run_checks.py starts.

run_checks.py puts this folder on PYTHONPATH, so Python imports it at startup
in the test module *and* in any Python subprocess the test spawns. It installs
an audit hook that refuses

  - file access under the forbidden roots (Chuen's real vault and memory
    folders, passed in DENISSE_CHECKS_FORBID), and
  - network connections, except to loopback ports other than Ollama's,

and logs every refusal so run_checks.py fails the module that tried. A test
that needs the vault, Ollama or the network must skip before reaching for it.

Nothing happens unless DENISSE_CHECKS_GUARD names the log file, so importing
this anywhere else is a no-op. DENISSE_CHECKS_GUARD_MODE=log records without
refusing (for auditing what a module touches).
"""
import os
import sys

_LOG = os.environ.get("DENISSE_CHECKS_GUARD")

_PATH_EVENTS = {"open": 1, "os.listdir": 1, "os.scandir": 1, "os.mkdir": 1,
                "os.remove": 1, "os.rmdir": 1, "os.chmod": 1, "os.truncate": 1,
                "os.utime": 1, "shutil.rmtree": 1, "os.rename": 2, "os.link": 2,
                "os.symlink": 2, "shutil.copyfile": 2, "shutil.move": 2}
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_OLLAMA_PORT = 11434


def _install():
    roots = [os.path.normcase(os.path.abspath(p)) for p in
             os.environ.get("DENISSE_CHECKS_FORBID", "").split(os.pathsep) if p]
    only_log = os.environ.get("DENISSE_CHECKS_GUARD_MODE") == "log"
    busy = []

    def forbidden(path):
        if path is None or isinstance(path, int):
            return False
        try:
            p = os.path.normcase(os.path.abspath(os.fsdecode(path)))
        except (TypeError, ValueError):
            return False
        return any(p == r or p.startswith(r + os.sep) for r in roots)

    def refuse(what):
        busy.append(1)
        try:
            with open(_LOG, "a", encoding="utf-8") as f:
                f.write(f"{what}  [pid {os.getpid()}: {' '.join(sys.argv)[:160]}]\n")
        finally:
            busy.pop()
        if not only_log:
            raise PermissionError(f"run_checks guard refused: {what}")

    def hook(event, args):
        if busy:
            return
        n = _PATH_EVENTS.get(event)
        if n:
            for a in args[:n]:
                if forbidden(a):
                    refuse(f"{event} {os.fsdecode(a)}")
        elif event == "socket.connect":
            addr = args[1]
            if isinstance(addr, tuple) and len(addr) >= 2:
                if addr[0] not in _LOOPBACK or addr[1] == _OLLAMA_PORT:
                    refuse(f"network connect to {addr[0]}:{addr[1]}")
            elif not isinstance(addr, (str, bytes)):  # AF_UNIX paths are local
                refuse(f"network connect to {addr!r}")

    sys.addaudithook(hook)


if _LOG:
    _install()
