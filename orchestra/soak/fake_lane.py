"""The soak's fake lane: a script the runner starts exactly like `claude -p`, steered by the
task file's `Soak:` header (a JSON plan written by harness.make_plans).

    python fake_lane.py --lane alpha      # a worker: sleeps, edits, commits, reports
    python fake_lane.py --reviewer        # the read-only reviewer: answers VERDICT: ok|block

Everything it does is also recorded as one JSON file per event under $SOAK_ROOT/events/, so
the harness can check invariants without trusting the runner's own log.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # orchestra/: proc_created() for the grandchild record

ROOT = Path(os.environ.get("SOAK_ROOT", "."))
GIT_ID = ["-c", "user.name=soak", "-c", "user.email=soak@example.invalid"]


def event(kind: str, **fields) -> None:
    (ROOT / "events").mkdir(parents=True, exist_ok=True)
    data = {"kind": kind, "t": time.time(), "pid": os.getpid(), **fields}
    path = ROOT / "events" / f"{time.time_ns()}-{os.getpid()}-{kind}.json"
    path.write_text(json.dumps(data), encoding="utf-8")


def plan_from(text: str) -> dict:
    m = re.search(r"^Soak:\s*(\{.*\})\s*$", text, re.M)
    return json.loads(m.group(1)) if m else {}


def git(*args: str) -> None:
    subprocess.run(["git", *GIT_ID, *args], check=True, capture_output=True)


def reviewer(prompt: str) -> int:
    plan = plan_from(prompt)
    verdict = "block" if plan.get("review") == "block" else "ok"
    event("REVIEW", slug=plan.get("slug"), verdict=verdict)
    time.sleep(0.2)
    print(json.dumps({"type": "result", "result": f"VERDICT: {verdict}\n- soak reviewer ({plan.get('slug')})",
                      "total_cost_usd": 0.02}))
    return 0


def claim_running(slug: str) -> None:
    """One lane per task at a time: an O_EXCL marker whose owner must be alive. A marker left by
    a killed lane is taken over; a live one means the runner started the same task twice."""
    from runner import _alive, proc_created
    running = ROOT / "running"
    running.mkdir(parents=True, exist_ok=True)
    marker = running / slug
    mine = json.dumps({"pid": os.getpid(), "created": proc_created(os.getpid())})
    for _ in range(3):
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, mine.encode("utf-8"))
            os.close(fd)
            return
        except FileExistsError:
            try:
                other = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                other = {}
            if other.get("pid") and _alive(other["pid"], other.get("created")):
                event("DUPLICATE", slug=slug, other_pid=other["pid"])
                return
            marker.unlink(missing_ok=True)  # a killed lane's leftover


def release_running(slug: str) -> None:
    (ROOT / "running" / slug).unlink(missing_ok=True)


def worker(lane: str, prompt: str) -> int:
    wt = Path.cwd()
    slug = wt.name
    task_text = Path(os.environ["ORCHESTRA_TASK"]).read_text(encoding="utf-8")
    plan = plan_from(task_text)
    event("START", slug=slug, lane=lane, plan=plan)
    claim_running(slug)
    try:
        time.sleep(float(plan.get("sleep", 1)))
        # One run only hits the "limit": the marker makes the retry go through.
        limited = ROOT / "limited"
        if plan.get("limit") and not (limited / slug).exists():
            limited.mkdir(parents=True, exist_ok=True)
            (limited / slug).write_text("1", encoding="utf-8")
            print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "resetsAt": int(time.time()) + int(plan.get("limit_s", 20)),
                "rateLimitType": "soak"}}))
            event("LIMIT", slug=slug)
            return 1
        if plan.get("grandchild"):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            from runner import proc_created
            (ROOT / "grandchildren").mkdir(parents=True, exist_ok=True)
            (ROOT / "grandchildren" / f"{child.pid}.json").write_text(
                json.dumps({"pid": child.pid, "created": proc_created(child.pid), "slug": slug}), encoding="utf-8")
            event("GRANDCHILD", slug=slug, child=child.pid)
        if plan.get("stray"):
            Path(os.environ["ORCHESTRA_MAIN"], f"stray_{slug}.txt").write_text("oops", encoding="utf-8")
        edits = []
        if plan.get("edit", True):
            p = wt / "work" / f"{slug}.txt"
            p.parent.mkdir(exist_ok=True)
            p.write_text(f"{slug} by {lane} at {time.time()}\n", encoding="utf-8")
            edits.append(p)
        if plan.get("hot"):
            hot = wt / "hot.md"
            hot.write_text(f"{slug} was here\n" + hot.read_text(encoding="utf-8"), encoding="utf-8")
            edits.append(hot)
        if plan.get("risky"):  # orchestra/ in the diff: the runner sends it to the reviewer
            p = wt / "orchestra" / "notes.txt"
            # U+201D and U+0141: bytes 0x9D / 0x81 in UTF-8, undefined in cp1252 (the runner's locale).
            # Built with chr() so this source file itself stays ASCII (the real reviewer reads the diff).
            p.write_text(f"{slug}: {chr(0x201C)}notes{chr(0x201D)} {chr(0x141)}\n", encoding="utf-8")
            edits.append(p)
        if plan.get("budget"):
            p = wt / "budget" / f"{slug}.txt"
            p.parent.mkdir(exist_ok=True)
            p.write_text("1\n", encoding="utf-8")
            edits.append(p)
        if plan.get("private"):  # aftercare must refuse to push this
            p = wt / "drafts" / f"{slug}.md"
            p.parent.mkdir(exist_ok=True)
            p.write_text("private-looking\n", encoding="utf-8")
            edits.append(p)
        if plan.get("bad_gate"):
            p = wt / "broken.flag"
            p.write_text("the gate fails on this\n", encoding="utf-8")
            edits.append(p)
        if edits:
            git("add", "--", *[str(p.relative_to(wt)).replace("\\", "/") for p in edits])
            git("commit", "-q", "-m", f"soak: {slug}")
            event("COMMIT", slug=slug, files=len(edits))
        result = plan.get("result", "PASS")
        if result != "NONE":
            Path(os.environ["ORCHESTRA_REPORT"]).write_text(
                f"RESULT: {result}\nGATE: soak gate -> {'pass' if result == 'PASS' else 'not run'}\n"
                f"HUMAN: {plan.get('human', 'none')}\nPREMISE: right\nDONE:\n- soak {slug}\nNOT DONE:\n- none\n"
                f"ODD:\n- none\nCOMMITS: {'one' if edits else 'none'}\n", encoding="utf-8")
        print(json.dumps({"type": "result", "total_cost_usd": float(plan.get("cost", 0.05)),
                          "session_id": f"soak-{slug}-{os.getpid()}"}))
        event("END", slug=slug, result=result)
        return 0
    finally:
        release_running(slug)


def main(argv: list[str]) -> int:
    # The runner writes the prompt on stdin (UTF-8); it must be drained. Bytes, not text: under the
    # console code page a curly quote in the reviewed diff would crash this fake, not the runner.
    prompt = sys.stdin.buffer.read().decode("utf-8", "replace")
    if "--reviewer" in argv:
        return reviewer(prompt)
    lane = argv[argv.index("--lane") + 1] if "--lane" in argv else "fake"
    return worker(lane, prompt)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
