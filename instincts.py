"""
Denisse -- instincts: what she learned from being corrected, used to pick
her tools.

The retrieval half of self-learning (vault: Denisse/Project Log/
Self-Learning.md). It stayed unbuilt because "a failure is not a lesson":
knowing that "study time" went wrong doesn't say what was right. A
correction that WORKED does. When Chuen corrects a turn and the next try
succeeds, the pair says both halves:

    "play lofi"   -> open_application, failed        <- bad
    "no, on spotify" -> play_song, succeeded         <- what worked
    => instinct: when he says "play", use play_song, not open_application

How an instinct changes what she does -- and the constraint that picks it:
NOTHING here enters her prompt (decisions.md, 2026-10-19: everything in the
prompt competes for an 8B model's attention, and lessons in it made her
measurably worse). An instinct only changes which tools tool_router offers
for a matching message:

  use    added to that turn's tools. Always safe -- the router already errs
         towards over-including -- so it applies from the first sighting.
  avoid  removed from that turn's tools. Withholding a tool can break a
         request that needed it, so it waits for MIN_AVOID sightings, and
         never removes a CORE tool.

Where they live: Denisse/Brain/instincts.json in the vault, beside
facts.json, with instincts.md as the readable copy. Not in this repo -- the
evidence quotes his real messages and the repo is public. In the vault, git
makes a bad instinct one revert away, and he (or any session) can switch one
off by setting "enabled": false, which a nightly rebuild keeps.

Rebuilt nightly from the whole stored history (scripts/nightly.py, stage 4),
so counts are recounted rather than accumulated: running it twice changes
nothing. DENISSE_INSTINCTS=0 turns the router hook off -- the control arm.

Run:
    python instincts.py          # rebuild from memory/conversation.json
    python instincts.py --dry    # show what it would learn, write nothing
"""

import datetime
import json
import os
import re

import learning

VAULT = os.environ.get("DENISSE_VAULT", r"C:\Users\Xichu\OneDrive - King's College London\Denisse")
BRAIN_DIR = os.path.join(VAULT, "Denisse", "Brain")
INSTINCTS_FILE = os.path.join(BRAIN_DIR, "instincts.json")
INSTINCTS_MD = os.path.join(BRAIN_DIR, "instincts.md")

MIN_AVOID = 2  # sightings before an instinct may withhold a tool
LOOKAHEAD = 2  # how many turns after a failure the fix may come

_STOP = set("""
a an the and or but to of in on at for with from by is are was were be been it
its this that these those i me my you your we us our he him his she her they
them please can could would will just now then so do does did not no nope
yeah yes ok okay hey hi denisse what whats how why when where who which
meant mean said wrong want wanted need like one some any all up out off
""".split())

_WORD = re.compile(r"[a-z][a-z']+")


def keywords(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2}


def _worked(turn: dict) -> bool:
    """Tools ran and none of them failed or came back empty."""
    if turn["label"] == "bad" or not turn["tools"] or not turn["results"]:
        return False
    return not any(learning.tool_failed(r) or (r or "").strip() in ("", "(no output)")
                   for r in turn["results"])


def derive(labelled: list) -> list:
    """Instincts from a labelled history (learning.label_turns)."""
    found = {}
    for i, bad in enumerate(labelled):
        if bad["label"] != "bad":
            continue
        for fix in labelled[i + 1:i + 1 + LOOKAHEAD]:
            if fix["label"] == "bad":
                continue  # corrected again -- keep looking, this wasn't it
            if not _worked(fix):
                break  # he moved on; nothing here says what was right
            use = sorted(set(fix["tools"]) - set(bad["tools"]))
            if not use:
                break
            # The trigger is what the failed message and its fix share --
            # "play lofi" / "no, play lofi on spotify" -> {"play", "lofi"}.
            # Sharing nothing means the next success was a new subject, not
            # a fix: the first dry run (2026-10-20) learned "turn it off ->
            # get_news" from exactly that, because he asked for the news next.
            keys = keywords(bad["user"]) & keywords(fix["user"])
            if not keys or len(keys) > 4:
                break  # unrelated, or too specific to recur
            avoid = sorted(set(bad["tools"]) - set(fix["tools"]))
            k = (tuple(sorted(keys)), tuple(use))
            entry = found.setdefault(k, {
                "when": sorted(keys), "use": use, "avoid": avoid, "count": 0,
                "evidence": {"failed": bad["user"][:120], "fixed_by": fix["user"][:120]},
            })
            entry["count"] += 1
            break
    return sorted(found.values(), key=lambda e: -e["count"])


def load(path: str | None = None) -> list:
    try:
        with open(path or INSTINCTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("instincts", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        return []


def _key(e):
    return (tuple(e["when"]), tuple(e["use"]))


def merge(old: list, new: list) -> list:
    """New counts win; his "enabled": false survives; an instinct whose
    evidence has since scrolled out of the history is kept as it was."""
    old_by = {_key(e): e for e in old}
    out = []
    for e in new:
        prev = old_by.pop(_key(e), None)
        e = dict(e)
        e["enabled"] = prev.get("enabled", True) if prev else True
        e["first_seen"] = (prev or {}).get("first_seen") or datetime.date.today().isoformat()
        out.append(e)
    out.extend(old_by.values())
    return out


def _md(instincts: list) -> str:
    lines = [
        "---",
        "name: instincts",
        'description: "What Denisse learned from being corrected - which tool to reach for. Generated nightly by instincts.py; edit instincts.json, not this."',
        "tags: [denisse, learning]",
        f"updated: {datetime.date.today().isoformat()}",
        "---",
        "# Instincts",
        "",
        "Learned from turns Chuen corrected where the retry worked. They only change",
        "**which tools she's offered** for a matching message -- nothing here enters her",
        f"prompt. `avoid` applies only after {MIN_AVOID} sightings. To switch one off, set",
        '`"enabled": false` in `instincts.json` (a rebuild keeps it). See [[Self-Learning]].',
        "",
    ]
    if not instincts:
        lines.append("_None yet._")
    else:
        lines += ["| when he says | use | avoid | seen | on |", "|---|---|---|---|---|"]
        for e in instincts:
            avoid = ", ".join(e["avoid"]) or "-"
            if e["avoid"] and e["count"] < MIN_AVOID:
                avoid += " (not yet)"
            lines.append(f"| {' + '.join(e['when'])} | {', '.join(e['use'])} | {avoid} "
                         f"| {e['count']} | {'yes' if e.get('enabled', True) else 'off'} |")
    return "\n".join(lines) + "\n"


def rebuild(history: list | None = None, dry: bool = False) -> list:
    if history is None:
        import memory_store
        history = memory_store.load_history()
    instincts = merge(load(), derive(learning.label_turns(history)))
    if not dry:
        os.makedirs(BRAIN_DIR, exist_ok=True)
        with open(INSTINCTS_FILE, "w", encoding="utf-8") as f:
            json.dump({"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                       "instincts": instincts}, f, indent=2, ensure_ascii=False)
        with open(INSTINCTS_MD, "w", encoding="utf-8") as f:
            f.write(_md(instincts))
    return instincts


# -- runtime: tool_router asks this ----------------------------------------

_cache = {"mtime": None, "items": []}


def enabled() -> bool:
    return os.environ.get("DENISSE_INSTINCTS", "1").lower() not in ("0", "false", "off", "no")


def _active() -> list:
    """The file, re-read only when it changes (the nightly rebuild)."""
    try:
        mtime = os.path.getmtime(INSTINCTS_FILE)
    except OSError:
        return []
    if mtime != _cache["mtime"]:
        _cache["mtime"], _cache["items"] = mtime, [e for e in load() if e.get("enabled", True)]
    return _cache["items"]


def adjust(allowed: set, text: str, protected: set = frozenset()) -> set:
    """Apply every instinct whose words all appear in `text`."""
    if not enabled():
        return allowed
    words = keywords(text)
    out = set(allowed)
    for e in _active():
        if not e.get("when") or not set(e["when"]) <= words:
            continue
        out |= set(e.get("use", []))
        if e.get("count", 0) >= MIN_AVOID:
            out -= set(e.get("avoid", [])) - set(protected) - set(e.get("use", []))
    return out


def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    dry = "--dry" in sys.argv
    got = rebuild(dry=dry)
    print(f"{len(got)} instinct(s){' (dry run, nothing written)' if dry else ''}")
    for e in got:
        print(f"  {' + '.join(e['when'])} -> use {e['use']}, avoid {e['avoid']} x{e['count']}"
              f"{'' if e.get('enabled', True) else ' (off)'}")
    if not dry:
        print(INSTINCTS_FILE)


if __name__ == "__main__":
    main()
