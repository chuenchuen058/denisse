"""The nightly vault job: check, flag, commit. One entry point.

Five stages, in this order:

  1. contract   -- runs vault_contract.py, which rewrites Denisse/Vault Health.md
  2. naming     -- names inside every Week N/ folder, against the convention
                   recorded in SEM 1 Modules/.keats-manifest.json
  3. flag       -- one open task per week note still at 'status: files-only',
                   so a missing write-up turns up on the reminder card
  4. propose    -- runs scripts/propose_cases.py, which turns yesterday's live
                   failures into DRAFT eval cases awaiting a human assertion
  5. commit     -- commits ONLY the files this job writes

What it deliberately does NOT do:

  * fetch from KEATS. Denisse does not sign in to KEATS at all -- the weekly
    pull is a scheduled Claude session driving Chuen's own browser session
    (Memory/decisions.md, 2026-10-19). Nothing here needs a Moodle token, and
    keats_login.py is not authorised; see the 2026-10-08 entry.
  * move or rename anything. Guessing which week a PDF belongs to, and what
    kind of file it is, is what produced the week-3 mess; this job reports
    names that break the convention and leaves them where they are.
  * write eval/cases.py. Stage 4 proposes; the assertion is a judgement and
    a wrong case is worse than no case.
  * git add -A. Chuen runs parallel sessions against these repos, so the
    commit stages an explicit list and nothing else.

Run it by hand with --dry to see everything except the commit.
"""
import io
import os
import re
import subprocess
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VAULT = r"C:\Users\Xichu\OneDrive - King's College London\Denisse"
MODULES = os.path.join(VAULT, "SEM 1 Modules")
LOG = os.path.join(REPO, "logs", "nightly.log")
DRY = "--dry" in sys.argv

# Inside a Week N/ folder the path already says the module and the week, so
# the filename only says what the file IS. See 'naming_convention' in the
# manifest; the leading-number variant was tried and rejected on 2026-10-12.
KINDS = ("Lecture", "Exercises", "Tutorial", "Notes", "Walkthrough", "Assignment")
NAME_OK = re.compile(r"^(?:%s)(?: \d+)? - .+" % "|".join(KINDS))
WEEK_DIR = re.compile(r"^Week (\d+)$")

report = []


def say(line):
    print(line)
    report.append(line)


# -- 1. the contract --------------------------------------------------------
say("== contract ==")
contract = subprocess.run(
    [sys.executable, os.path.join(HERE, "vault_contract.py")],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
summary = [l for l in contract.stdout.splitlines()
           if l.startswith(("errors", "warnings", "files-only", "unlinked", "95", "report"))
           or re.match(r"^\d+ notes", l)]
for l in summary:
    say("  " + l)
if contract.returncode:
    say("  -> errors above; see Denisse/Vault Health.md")

# -- 2. names inside the week folders --------------------------------------
say("== naming ==")
odd = 0
for module in sorted(os.listdir(MODULES)):
    mpath = os.path.join(MODULES, module)
    if not os.path.isdir(mpath):
        continue
    for sub in sorted(os.listdir(mpath)):
        m = WEEK_DIR.match(sub)
        if not m:
            continue
        week, wpath = m.group(1), os.path.join(mpath, sub)
        # '5CCYB070 Computational Methods (26~27 SY)' -> 'Computational Methods':
        # the code and KEATS's year suffix are both folder-only.
        title = re.sub(r"\s*\([^)]*\)\s*$", "", module.split(" ", 1)[1]).strip()
        expect_note = f"{title} Week {week}.md"
        for fn in sorted(os.listdir(wpath)):
            # .m and .mat come down from KEATS as an exercise's own assets --
            # a script and the data it loads. Their names belong to the
            # exercise (ex1_data.mat is loaded by name), so the convention
            # does not apply and renaming them would break the exercise.
            if (fn == expect_note or fn.startswith(".")
                    or fn.endswith((".m", ".mat"))):
                continue
            if not NAME_OK.match(fn):
                say(f"  off-convention  {module}/{sub}/{fn}")
                odd += 1
        if not os.path.exists(os.path.join(wpath, expect_note)):
            say(f"  no week note    {module}/{sub}/ (expected '{expect_note}')")
            odd += 1
if not odd:
    say("  every week folder matches the convention")

# -- 3. flag the write-ups that haven't happened ----------------------------
say("== flag ==")
sys.path.insert(0, REPO)
import tasks  # noqa: E402  (after sys.path is set)

pending = []
for dirpath, dirnames, filenames in os.walk(MODULES):
    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
    for fn in filenames:
        if not fn.endswith(".md"):
            continue
        with open(os.path.join(dirpath, fn), encoding="utf-8-sig") as f:
            head = f.read(1200)
        if re.search(r"^status: files-only\s*$", head, re.M):
            pending.append(fn[:-3])

open_text = {t["text"].lower() for t in tasks.list_tasks()}
for note in sorted(pending):
    want = f"Write up {note}"
    if want.lower() in open_text:
        say(f"  already flagged  {note}")
    elif DRY:
        say(f"  would flag       {note}")
    else:
        tasks.add_task(want, "", "low")
        say(f"  flagged          {note}")
if not pending:
    say("  no week note is waiting on a write-up")

# -- 4. promote live failures into draft cases ------------------------------
# This closes the loop the roadmap asked for ("every bug found becomes a
# case"): learning.py labels live turns, and until now a human had to read the
# eval report and remember. Drafts only -- the assertion is a judgement, so
# nothing lands in eval/cases.py without being looked at. The output quotes
# his real conversation, so eval/proposed_cases.py is gitignored; the repo is
# public.
say("== propose ==")
# Refresh learned.json first, or the drafts are yesterday's. learning.py only
# reads the stored history; the write goes to gitignored memory/learned.json.
learner = subprocess.run(
    [sys.executable, os.path.join(REPO, "learning.py")], cwd=REPO,
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
if learner.returncode:
    say("  learning.py exited %d -- drafts may be stale" % learner.returncode)
    say("  " + (learner.stderr.strip().splitlines() or ["no stderr"])[-1])
else:
    for l in learner.stdout.splitlines():
        if "live turns" in l or "distinct failing" in l:
            say("  " + l.lstrip("- ").strip())

proposer = os.path.join(HERE, "propose_cases.py")
if not os.path.exists(proposer):
    say("  propose_cases.py is missing -- skipped")
    drafts = -1
else:
    # --write even on --dry: the file is a gitignored scratch draft, not a
    # change to the vault or to the suite, and seeing it is the point.
    pc = subprocess.run(
        [sys.executable, proposer, "--write"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    m = re.search(r"(\d+) draft case\(s\) need a human decision", pc.stdout)
    drafts = int(m.group(1)) if m else 0
    for l in pc.stdout.splitlines():
        if re.search(r"learned\.json:|already covers|already covered|"
                     r"need a human decision|caught up|wrote |no memory/", l):
            say("  " + l.strip())
    if pc.returncode:
        say(f"  propose_cases.py exited {pc.returncode}")
        say("  " + (pc.stderr.strip().splitlines() or ["no stderr"])[-1])
    elif drafts:
        say("  -> decide each assertion, then move it into eval/cases.py by hand")

# -- the stage that isn't running -------------------------------------------
say("== keats ==")
say("  not this job's work: the weekly pull is a scheduled Claude session")
say("  driving Chuen's own browser (Memory/decisions.md, 2026-10-19).")
say("  Nothing here fetches, and no Moodle token is involved.")

# -- 5. commit, narrowly ----------------------------------------------------
say("== commit ==")
MINE = ["Denisse/Vault Health.md", "Denisse/Tasks.md"]


def git(*args, cwd=VAULT):
    return subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


staged = [p for p in MINE if os.path.exists(os.path.join(VAULT, p.replace("/", os.sep)))]
dirty = [p for p in staged if git("status", "--short", "--", p).stdout.strip()]
if not dirty:
    say("  nothing of this job's own changed")
elif DRY:
    say(f"  would commit {', '.join(dirty)}")
else:
    for p in dirty:
        git("add", "--", p)
    msg = ("nightly: vault health" + (" and write-up flags" if pending else "")
           + f"\n\nAutomated run {datetime.now():%Y-%m-%d %H:%M}. Only this job's"
             "\nown files are staged -- parallel sessions own everything else.")
    r = git("-c", "user.name=Denisse", "-c", "user.email=denisse@local", "commit", "-m", msg)
    say("  " + (r.stdout.strip().splitlines() or ["commit produced no output"])[0])

# -- a trail ----------------------------------------------------------------
line = (f"{datetime.now():%Y-%m-%d %H:%M}  contract_rc={contract.returncode} "
        f"off_convention={odd} pending_writeups={len(pending)} "
        f"draft_cases={drafts}{' DRY' if DRY else ''}")
os.makedirs(os.path.dirname(LOG), exist_ok=True)
with open(LOG, "a", encoding="utf-8") as f:
    f.write(line + "\n")
print("\n" + line)
sys.exit(0)
