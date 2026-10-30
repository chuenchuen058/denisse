"""Feedback-validated, non-parametric learning for Denisse's tool router.

After each saved turn (and nightly), a failed request followed by an
explicit correction, non-failing tools and positive human feedback creates
a candidate lesson. Two distinct accepted fixes are required for activation.
Evidence IDs survive history truncation; rereading never inflates counts.
Observed negatives can demote lessons below an 80% positive-feedback ratio.

Lessons match the original phrase (ignoring polite fillers), prioritise
available tools and may add read-only tools. They never grant new mutation
permissions, remove existing tools, rewrite source/prompts or train weights.
Manual enabled:false is preserved. Legacy unvalidated lessons stay inactive.
Private data lives in Denisse/Brain/instincts.json + .md in the vault.
Set self_learning:false in memory/brain_rules.json, or DENISSE_INSTINCTS=0,
to freeze learning and disable its routing effects.

    python instincts.py --dry    # inspect without writing
    python instincts.py          # observe saved history
"""

import datetime
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager

import brain_rules
import learning

VAULT = os.environ.get("DENISSE_VAULT", r"C:\Users\Xichu\OneDrive - King's College London\Denisse")
BRAIN_DIR = os.path.join(VAULT, "Denisse", "Brain")
INSTINCTS_FILE = os.path.join(BRAIN_DIR, "instincts.json")
INSTINCTS_MD = os.path.join(BRAIN_DIR, "instincts.md")

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


def _ran(turn: dict) -> list:
    """(tool, result) pairs that actually ran. A "Refused:" result is a call
    the brain blocked before running -- since 2026-10-30 a repair refuses an
    unchanged repeat of the failed call -- so it is evidence neither way."""
    return [(t, r) for t, r in zip(turn["tools"], turn["results"])
            if not (r or "").startswith("Refused")]


def _worked(turn: dict) -> bool:
    """Positive human feedback AND non-failing tool evidence, not silence."""
    ran = _ran(turn)
    if turn["label"] != "good" or not ran:
        return False
    return not any(learning.tool_failed(r) or (r or "").strip() in ("", "(no output)")
                   or (r or "").startswith(("User declined", "No timer started"))
                   or "nothing was stopped" in (r or "").lower()
                   for _, r in ran)


def phrase(text):
    return " ".join(w for w in _WORD.findall((text or "").lower())
                    if w not in {"please", "denisse", "hey"})


def derive(labelled: list) -> list:
    """Instincts from a labelled history (learning.label_turns)."""
    try:
        from brain import _COMPLAINT
    except Exception:
        _COMPLAINT = None
    import tool_router
    found = {}
    for i, bad in enumerate(labelled):
        if bad["label"] != "bad":
            continue
        text = bad["user"].strip()
        if tool_router.feedback_only(text) or (_COMPLAINT is not None and _COMPLAINT.fullmatch(text)):
            # A complaint that was itself complained about is not a request:
            # keying a lesson on "thats not what im asking" would reroute
            # every later complaint. The original request carries the lesson.
            continue
        for j, fix in enumerate(labelled[i + 1:i + 1 + LOOKAHEAD], start=i + 1):
            if fix["user"].strip().lower() == "/bad":
                continue  # explicit feedback can precede the corrected command
            if fix["label"] == "bad":
                continue  # corrected again -- keep looking, this wasn't it
            if not _worked(fix):
                break  # he moved on; nothing here says what was right
            linked_feedback = (j > i + 1 and labelled[j - 1]["user"].strip().lower() == "/bad"
                               and bool(keywords(bad["user"]) & keywords(fix["user"])))
            if not learning._CORRECTION.search(fix["user"]) and not linked_feedback:
                break  # a successful new subject is not a validated correction
            use = sorted({t for t, _ in _ran(fix)} - set(bad["tools"]))
            if not use:
                break
            # An explicit correction plus positive human feedback links the
            # turns. Match the original phrase rather than generalising from
            # a shared word such as "timer" to every timer operation.
            keys = keywords(bad["user"])
            if not keys:
                break  # unrelated, or too specific to recur
            avoid = sorted(set(bad["tools"]) - set(fix["tools"]))
            trigger = phrase(bad["user"])
            import vault_search
            overview = "search_vault" in use and vault_search.is_overview_query(fix["user"])
            k = (trigger, tuple(use), overview)
            entry = found.setdefault(k, {
                "when": sorted(keys)[:4], "phrase": trigger, "use": use, "avoid": avoid,
                "vault_overview": overview,
                "count": 0, "validated": True, "evidence_ids": [],
                "evidence": {"failed": bad["user"][:120], "fixed_by": fix["user"][:120]},
            })
            # Several failed attempts before ONE accepted fix still count
            # as one confirmation, not several independent experiments.
            identity = (["accepted_fix", fix["id"]] if fix.get("id") else
                        [bad["user"], fix["user"], fix["tools"], fix["results"]])
            evidence_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            if evidence_id not in entry["evidence_ids"]:
                entry["evidence_ids"].append(evidence_id)
            entry["count"] = len(entry["evidence_ids"])
            break
    return sorted(found.values(), key=lambda e: -e["count"])


def load(path: str | None = None) -> list:
    try:
        with open(path or INSTINCTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("instincts", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            return []
        clean = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if not all(isinstance(item.get(k, []), list) and
                       all(isinstance(v, str) for v in item.get(k, []))
                       for k in ("when", "use", "avoid", "evidence_ids", "negative_ids")):
                continue
            if type(item.get("enabled", True)) is not bool:
                continue
            if not isinstance(item.get("phrase", ""), str):
                continue
            if type(item.get("count", 0)) is not int or item.get("count", 0) < 0:
                continue
            e = dict(item)
            for key in ("when", "use", "avoid"):
                e.setdefault(key, [])
            e.setdefault("count", len(set(e.get("evidence_ids", []))))
            clean.append(e)
        return clean
    except (OSError, ValueError):
        return []


def _key(e):
    return (e.get("phrase") or tuple(e.get("when", [])), tuple(e.get("use", [])),
            e.get("vault_overview") is True)


def merge(old: list, new: list) -> list:
    """Union stable evidence IDs. Re-observation never fabricates new trials."""
    old_by = {_key(e): e for e in old}
    out = []
    for e in new:
        prev = old_by.pop(_key(e), None)
        if prev is None:
            legacy_key = next((k for k, old_item in old_by.items()
                               if not old_item.get("phrase") and old_item.get("when")
                               and set(old_item["when"]) <= set(e["when"])
                               and old_item.get("use") == e.get("use")), None)
            if legacy_key is not None:
                prev = old_by.pop(legacy_key)
        e = dict(e)
        e["enabled"] = prev.get("enabled", True) if prev else True
        if prev and e.get("validated"):
            e["evidence_ids"] = sorted(set(prev.get("evidence_ids", [])) | set(e["evidence_ids"]))[-128:]
            e["count"] = len(e["evidence_ids"])
            e["negative_ids"] = prev.get("negative_ids", [])
        e["first_seen"] = (prev or {}).get("first_seen") or datetime.date.today().isoformat()
        out.append(e)
    out.extend(dict(e) for e in old_by.values())
    return out


def _md(instincts: list) -> str:
    lines = [
        "---",
        "name: instincts",
        'description: "What Denisse learned from being corrected - which tool to reach for. Generated after saved turns and nightly; edit instincts.json, not this."',
        "tags: [denisse, learning]",
        f"updated: {datetime.date.today().isoformat()}",
        "---",
        "# Instincts",
        "",
        "Learned from turns Chuen corrected where the retry worked. They only change",
        "**which tools she's offered** for a matching message -- nothing here enters her",
        f"prompt. Lessons activate after {brain_rules.load().learning_confirmations} positively confirmed episodes.",
        "They prioritise tools; they never grant new machine-changing permissions or remove existing tools. To switch one off, set",
        '`"enabled": false` in `instincts.json` (a rebuild keeps it). See [[Self-Learning]].',
        "",
    ]
    if not instincts:
        lines.append("_None yet._")
    else:
        lines += ["| when he says | use | avoid | seen | on |", "|---|---|---|---|---|"]
        for e in instincts:
            state = ('off' if not e.get('enabled', True) else 'active' if _validated(e) else 'candidate')
            lines.append(f"| {e.get('phrase') or ' + '.join(e['when'])} | {', '.join(e['use'])} | recorded only "
                         f"| {e['count']} | {state} |")
    return "\n".join(lines) + "\n"


_write_lock = threading.RLock()


@contextmanager
def _store_lock():
    """Coordinate the tray and nightly rebuild; never write partial JSON."""
    os.makedirs(os.path.dirname(INSTINCTS_FILE), exist_ok=True)
    with _write_lock, open(INSTINCTS_FILE + ".lock", "a+b") as lock:
        if os.path.getsize(lock.name) == 0:
            lock.write(b"0"); lock.flush()
        acquired = False
        for _ in range(20):
            try:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                time.sleep(.025)
        if not acquired:
            raise TimeoutError("learning store is busy")
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _atomic_write(path, content):
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path), delete=False) as f:
            temp = f.name
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            os.unlink(temp)


def rebuild(history: list | None = None, dry: bool = False) -> list:
    if not enabled():
        return load()
    if history is None:
        import memory_store
        history = memory_store.load_history()
    labelled = learning.label_turns(history)
    derived = derive(labelled)
    if dry:
        return merge(load(), derived)
    with _store_lock():
        old = load()
        items = merge(old, derived)
        for item in items:
            negative_ids = set(item.get("negative_ids", []))
            for turn in labelled:
                if (turn["label"] == "bad" and item.get("phrase") == phrase(turn["user"])
                        and set(item.get("use", [])) & set(turn["tools"])):
                    identity = turn.get("id") or [turn["user"], turn["tools"], turn["results"]]
                    negative_ids.add(hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest())
            if negative_ids:
                item["negative_ids"] = sorted(negative_ids)[-128:]
        if items != old or not os.path.exists(INSTINCTS_FILE):
            _atomic_write(INSTINCTS_FILE, json.dumps({"updated": datetime.datetime.now().isoformat(timespec="seconds"),
                                                      "instincts": items}, indent=2, ensure_ascii=False))
        rendered = _md(items)
        try:
            with open(INSTINCTS_MD, encoding="utf-8") as f:
                previous_md = f.read()
        except OSError:
            previous_md = None
        if rendered != previous_md:
            _atomic_write(INSTINCTS_MD, rendered)
    return items


# -- runtime: tool_router asks this ----------------------------------------

_cache = {"mtime": None, "items": []}


def enabled() -> bool:
    return brain_rules.load().self_learning and os.environ.get("DENISSE_INSTINCTS", "1").lower() not in ("0", "false", "off", "no")


def _validated(e):
    positives = len(set(e.get("evidence_ids", [])))
    negatives = len(set(e.get("negative_ids", [])))
    return (e.get("validated") is True and e.get("enabled", True)
            and positives >= brain_rules.load().learning_confirmations
            and positives >= 4 * negatives)  # at least 80% observed positive feedback


def _active() -> list:
    """The private store, re-read when an observation or manual edit changes it."""
    try:
        mtime = (INSTINCTS_FILE, os.stat(INSTINCTS_FILE).st_mtime_ns)
    except OSError:
        return []
    if mtime != _cache["mtime"]:
        _cache["mtime"], _cache["items"] = mtime, [e for e in load() if e.get("enabled", True)]
    return _cache["items"]


def adjust(allowed: set, text: str, protected: set = frozenset()) -> set:
    """Confirmed exact phrases may add read tools, never new action powers."""
    if not enabled():
        return allowed
    out = set(allowed)
    for e in _active():
        if not _validated(e) or not e.get("phrase") or e["phrase"] != phrase(text):
            continue
        out |= set(e.get("use", [])) - brain_rules.MUTATING_TOOLS
    return out


def preferred(text, allowed):
    if not enabled():
        return set()
    return {name for e in _active() if _validated(e) and e.get("phrase") == phrase(text)
            for name in e.get("use", []) if name in allowed}


def wants_vault_overview(text):
    """A confirmed correction can select the existing read-only overview.

    This stores a supported intent, never arbitrary tool arguments or code.
    Conflicting confirmed meanings leave the model's query unchanged.
    """
    if not enabled():
        return False
    matches = [e for e in _active() if _validated(e) and e.get("phrase") == phrase(text)
               and "search_vault" in e.get("use", [])]
    return bool(matches) and all(e.get("vault_overview") is True for e in matches)


def status():
    items = load()
    return {"enabled": enabled(), "confirmed_lessons": sum(bool(_validated(e)) for e in items),
            "candidates": sum(bool(e.get("enabled", True) and not _validated(e)) for e in items),
            "disabled_lessons": sum(not e.get("enabled", True) for e in items),
            "confirmations_required": brain_rules.load().learning_confirmations,
            "store": INSTINCTS_FILE}


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
