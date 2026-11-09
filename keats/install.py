"""Preview a daily Windows task; only --register changes Task Scheduler."""
import argparse
import datetime as dt
import getpass
import os
from pathlib import Path
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from . import conf


def task_xml(config, repo=None, now=None, user=None):
    c = config["schedule"]
    repo = Path(repo or Path(__file__).resolve().parents[1]).resolve()
    now = now or dt.datetime.now()
    clock = dt.time.fromisoformat(c["time"])
    start = dt.datetime.combine(now.date(), clock)
    if start <= now:
        start += dt.timedelta(days=1)
    user = user or "\\".join(v for v in (os.environ.get("USERDOMAIN"), getpass.getuser()) if v)
    task = ET.Element("Task", version="1.2", xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task")

    def child(parent, tag, text=None, **attrs):
        item = ET.SubElement(parent, tag, attrs)
        item.text = text
        return item

    trigger = child(child(task, "Triggers"), "CalendarTrigger")
    child(trigger, "StartBoundary", start.isoformat(timespec="seconds"))
    child(trigger, "Enabled", "true")
    child(child(trigger, "ScheduleByDay"), "DaysInterval", "1")
    principal = child(child(task, "Principals"), "Principal", id="Author")
    child(principal, "UserId", user)
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(task, "Settings")
    for key, value in {"MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "true",
                       "StopIfGoingOnBatteries": "true", "StartWhenAvailable": "true",
                       "RunOnlyIfNetworkAvailable": "true", "Enabled": "true",
                       "ExecutionTimeLimit": c["execution_limit"]}.items():
        child(settings, key, value)
    action = child(child(task, "Actions", Context="Author"), "Exec")
    child(action, "Command", str(repo / c["python"]))
    child(action, "Arguments", subprocess.list2cmdline([str(repo / c["script"])]))
    child(action, "WorkingDirectory", str(repo))
    return ET.tostring(task, encoding="unicode", xml_declaration=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", action="store_true")
    args = parser.parse_args(argv)
    config = conf.load()
    xml = task_xml(config)
    print(xml)
    if not args.register:
        return 0
    # Use a temporary XML, so names/paths are never interpolated into shell code.
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / config["schedule"]["xml_file"]
        path.write_text(xml, encoding="utf-16")
        c = config["schedule"]
        command = [part.format(name=c["task_name"], xml=str(path)) for part in c["register_command"]]
        return subprocess.run(command, timeout=c["register_timeout_s"]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
