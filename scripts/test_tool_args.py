"""Denisse said a file wasn't on the machine. It was.

2026-10-26, asked to find a passport sitting in
OneDrive\\Documents\\Resume\\IUMC, she reported it missing four times.
Three separate faults, none of them the model being stupid:

  1. find_files(filename=...) -- the model got the argument NAME wrong,
     _drop_unexpected_args discarded the value, and the call died with
     "missing 1 required positional argument: 'query'".
  2. That TypeError told the model the call failed but not how to fix
     it, so it stopped calling and reported the file as not found.
  3. Windows redirects Documents and Desktop into OneDrive and leaves
     empty stubs behind. _default_search_dirs setdefault'd the stubs and
     dropped the real folders, so even a correct call searched nowhere
     useful.

Run after touching _default_search_dirs, _rescue_arg_names or _run_tool.

Runs against a fake home that rebuilds that machine's layout (an empty
Documents stub beside OneDrive\\Documents\\Resume\\IUMC), never the real
one: it used to search Chuen's own folders, vault included.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="denisse-toolargs-")
os.environ.setdefault("DENISSE_VAULT", os.path.join(TMP, "vault"))

import brain  # noqa: E402
import tasks  # noqa: E402
import tools  # noqa: E402

tasks.TASKS_FILE = os.path.join(TMP, "tasks.json")
FAKE_HOME = os.path.join(TMP, "home")
for rel in ("Documents", "Desktop", "Downloads", os.path.join("OneDrive", "Desktop"),
            os.path.join("OneDrive", "Documents", "Resume", "IUMC")):
    os.makedirs(os.path.join(FAKE_HOME, rel), exist_ok=True)
with open(os.path.join(FAKE_HOME, "OneDrive", "Documents", "Resume", "IUMC", "Passport scan.pdf"), "wb") as f:
    f.write(b"%PDF-1.4 synthetic")
os.environ["USERPROFILE"] = os.environ["HOME"] = FAKE_HOME

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


dirs = tools._default_search_dirs()

# 1. The redirected folders are both present, under distinct keys.
home = os.path.expanduser("~")
od_docs = [k for k, v in dirs.items()
           if v.lower().startswith(os.path.join(home, "onedrive").lower())
           and v.lower().rstrip("\\").endswith("documents")]
check("OneDrive Documents is searchable", bool(od_docs),
      f"keys={od_docs or sorted(dirs)}")

# 2. No folder is registered twice, and none was lost to a name clash.
paths = [os.path.normcase(p) for p in dirs.values()]
check("no duplicate search roots", len(paths) == len(set(paths)))

# 3. location='documents' must reach the OneDrive copy, not just the stub.
out = tools.find_files("passport", location="documents")
check("location='documents' finds the passport", "Passport" in out,
      out.splitlines()[0][:90] if out else "")

# 4. The whole-machine search finds it too.
out = tools.find_files("passport")
check("unscoped search finds the passport", "Passport" in out,
      out.splitlines()[0][:90] if out else "")

# 5. The wrong argument name is repaired rather than thrown away.
f = tools.TOOL_FUNCTIONS["find_files"]
for wrong in ("filename", "name", "file"):
    fixed = brain._rescue_arg_names(f, {wrong: "passport"})
    check(f"find_files({wrong}=...) is rescued to query",
          fixed.get("query") == "passport", f"got {fixed}")

# 6. A genuinely empty call gets an instruction, not a bare TypeError.
out = brain._run_tool("find_files", {}, None)
check("empty call explains what to send",
      "query" in out and "Call 'find_files' again" in out, out[:90])

# 7. The 2026-09-23 behaviour still holds: a spurious argument to a tool
#    that needs none is dropped, and the call still runs.
out = brain._run_tool("list_tasks", {"task": "the N20 motor"}, None)
check("spurious arg still dropped, call still runs",
      "Error running" not in out, out[:70])

# 8. Ambiguity is not guessed at: two dropped values, one hole -> no repair.
fixed = brain._rescue_arg_names(f, {"a": "x", "b": "y"})
check("ambiguous names are not guessed", "query" not in fixed, f"got {fixed}")

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
