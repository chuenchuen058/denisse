# Adds the Atrium capture hook to Claude Code's user settings (~/.claude/settings.json).
# Backs the file up, keeps every existing key, and only adds three hook entries
# (Stop, PreCompact, SessionEnd) if they aren't there yet. Safe to run twice.
import json, os, shutil, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import settings  # noqa: E402  (the repo root, found from this file)
s = Path.home() / ".claude" / "settings.json"
data = json.loads(s.read_text(encoding="utf-8")) if s.exists() else {}
if s.exists():
    shutil.copyfile(s, str(s) + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
# Forward slashes and no `cd`: Claude Code on Windows may run hooks through Git Bash,
# where `cd /d` fails silently (found 2 Nov 2026).
repo = Path(settings.repo()).as_posix()
cmd = f'"{repo}/.venv/Scripts/python.exe" "{repo}/atrium/hook.py"'
hooks = data.setdefault("hooks", {})
for ev in ("Stop", "PreCompact", "SessionEnd"):
    lst = hooks.setdefault(ev, [])
    lst[:] = [e for e in lst if not any("atrium capture" in h.get("command", "") for h in e.get("hooks", []))]
    if not any("atrium/hook.py" in h.get("command", "") for e in lst for h in e.get("hooks", [])):
        lst.append({"matcher": "*" if ev != "Stop" else "", "hooks": [{"type": "command", "command": cmd, "timeout": 20}]})
tmp = str(s) + ".tmp"
s.parent.mkdir(parents=True, exist_ok=True)
Path(tmp).write_text(json.dumps(data, indent=2), encoding="utf-8")
os.replace(tmp, s)
print("hooks now:", {k: len(v) for k, v in data["hooks"].items()}, "| other keys kept:", [k for k in data if k != "hooks"])
