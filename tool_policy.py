"""
The tool policy: what each tool may do, which tools a turn is offered, and
the few phrase lists that still run before the model. All of it is data in
tool_policy.json; this module only reads it.

Why (AGENTS.md Rule 0, 2026-11-03): brain.execute() used to decide whether
a call was allowed by matching regexes against what Chuen had typed --
get_current_time needed a time word in his message, add_task needed "remind"
or "task", find_files was refused if the router hadn't offered it. On the
evening of 3 Nov that refused the file search that would have found his
syllabus, turned "don't show me the file path" into a task, and told him
"No tool needed" for "session over". Understanding what he means is the
model's job, given good tool descriptions; the code's job is to check the
CALL -- its arguments and side effects -- and never his wording.

So a call is refused only when:
  - the tool doesn't exist;
  - it changes something ("write") and wasn't offered this turn, or this is
    a complaint turn (the router keeps mutating tools out of those);
  - one of the tool's own `checks` fails on its arguments: a destructive
    shell command, a write into Denisse's own folder, opening a document
    path nothing verified, a "calculation" with nothing to calculate, a
    skill that doesn't exist.
A read-only tool may always be called, offered or not: a lookup the model
decides it needs is never the thing to block.

DENISSE_TOOL_POLICY points at another policy file (the checks sandbox, A/Bs).
The file is re-read when it changes, so an edit takes effect without a
restart; an invalid edit keeps the last good policy and logs why.
"""

import json
import logging
import ntpath
import os
import re
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(ROOT, "tool_policy.json")

_lock = threading.RLock()
_cache = {"path": None, "mtime": None, "data": None}
_compiled = {}


def path() -> str:
    return os.environ.get("DENISSE_TOOL_POLICY") or DEFAULT_FILE


def _validate(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("policy must be a JSON object")
    for key in ("core", "groups", "cues", "study_commands", "tools"):
        if key not in data:
            raise ValueError(f"policy is missing '{key}'")
    for name, meta in data["tools"].items():
        if meta.get("side_effect") not in ("read", "write"):
            raise ValueError(f"tools.{name}.side_effect must be 'read' or 'write'")
        for chk in meta.get("checks", []):
            if "kind" not in chk or "argument" not in chk or "refusal" not in chk:
                raise ValueError(f"tools.{name}: every check needs kind, argument and refusal")
    for name, g in data["groups"].items():
        if not isinstance(g.get("tools"), list) or not isinstance(g.get("cues"), str):
            raise ValueError(f"groups.{name} needs a tools list and a cues pattern")
        re.compile(g["cues"], re.IGNORECASE)
    for name, p in data["cues"].items():
        re.compile(p, re.IGNORECASE)
    return data


def load() -> dict:
    p = path()
    with _lock:
        try:
            mtime = os.stat(p).st_mtime_ns
        except OSError as exc:
            if _cache["data"] is not None:
                return _cache["data"]
            raise RuntimeError(f"tool policy missing: {p} ({exc})")
        if _cache["path"] == p and _cache["mtime"] == mtime:
            return _cache["data"]
        try:
            with open(p, encoding="utf-8-sig") as f:
                data = _validate(json.load(f))
        except (OSError, ValueError) as exc:
            if _cache["data"] is not None:
                logging.warning("tool policy edit ignored: %s", exc)
                _cache["mtime"] = mtime
                return _cache["data"]
            raise RuntimeError(f"tool policy unreadable: {p}: {exc}")
        _cache.update(path=p, mtime=mtime, data=data)
        _compiled.clear()
        return data


def _rx(pattern: str) -> re.Pattern:
    rx = _compiled.get(pattern)
    if rx is None:
        rx = _compiled[pattern] = re.compile(pattern, re.IGNORECASE)
    return rx


def cue(name: str) -> re.Pattern:
    """A named phrase pattern from `cues` -- compiled, case-insensitive."""
    return _rx(load()["cues"][name])


def groups() -> dict:
    """group name -> (set of tool names, compiled cue pattern)."""
    return {name: (set(g["tools"]), _rx(g["cues"])) for name, g in load()["groups"].items()}


def core() -> set:
    return set(load()["core"])


def skills_tools() -> set:
    return set(load().get("skills_tools", ()))


def desktop_tools() -> set:
    return set(load().get("desktop_tools", ()))


def study_commands() -> dict:
    return dict(load()["study_commands"])


def tool(name: str) -> dict:
    return load()["tools"].get(name, {})


def known(name: str) -> bool:
    return name in load()["tools"]


def side_effect(name: str) -> str:
    """'read' or 'write'. A tool the policy doesn't know is treated as
    'write' -- the conservative reading -- and brain.execute() refuses
    names that aren't real tools before it gets here anyway."""
    return tool(name).get("side_effect", "write")


def mutating() -> frozenset:
    """Every tool that changes something. The permission ceiling for learned
    routing preferences (instincts.py) and the honesty net's idea of what
    counts as an action having happened."""
    return frozenset(n for n, m in load()["tools"].items() if m.get("side_effect") != "read")


def once_per_turn() -> frozenset:
    return frozenset(n for n, m in load()["tools"].items() if m.get("once_per_turn"))


def allowed_unoffered(name: str) -> bool:
    """May this tool run when the router didn't offer it this turn?"""
    rule = load().get("unoffered", {}).get(side_effect(name), "refuse")
    return rule == "allow"


def _inside(folder: str, p: str) -> bool:
    try:
        p = os.path.normcase(os.path.abspath(os.path.expanduser(p or "")))
        folder = os.path.normcase(folder)
        return p == folder or p.startswith(folder + os.sep)
    except Exception:
        return True


def _own_file_name(folder: str, p: str, names=()) -> bool:
    """Whether `p` names one of Denisse's own files, wherever the path puts
    it: a file that exists at the top of her program folder (by name), or a
    file with one of the check's `names` as a word of its name ('settings',
    'config', 'denisse' -- "Denisse settings.txt" under AppData was the
    model's third invention, 2026-11-04)."""
    try:
        name = ntpath.basename(p.strip().strip('"').replace("/", "\\")).lower()
        if not name:
            return False
        stem = name.rsplit(".", 1)[0]
        if set(re.findall(r"[a-z0-9]+", stem)) & {n.lower() for n in names}:
            return True
        return any(name == f.lower() and os.path.isfile(os.path.join(folder, f))
                   for f in os.listdir(folder))
    except OSError:
        return False


def _only_a_time(text: str) -> bool:
    try:
        import time_parse
        return time_parse.only_a_time(text)
    except Exception:
        return False


def _time_question(text: str) -> str:
    if not (text or "").strip():
        return ""
    try:
        import time_parse
        return time_parse.parse(text).question or ""
    except Exception:
        return ""


def _arg(args: dict, which) -> str:
    names = which if isinstance(which, list) else [which]
    for n in names:
        v = (args or {}).get(n)
        if v not in (None, ""):
            return str(v)
    return ""


def refuse(name: str, args: dict, *, verified_path=None, known_skill=None,
           program_folder: str = ROOT) -> str | None:
    """The refusal for this call, or None when it may run. Every check is on
    the call's own arguments; `verified_path(p)` and `known_skill(n)` are
    the two facts the caller knows and the policy doesn't."""
    for chk in tool(name).get("checks", []):
        value = _arg(args, chk["argument"])
        kind = chk["kind"]
        refusal = chk["refusal"].replace("{value}", value)
        if kind == "must_not_match":
            if value and _rx(chk["pattern"]).search(value):
                return refusal
        elif kind == "must_match":
            if not _rx(chk["pattern"]).search(value):
                return refusal
        elif kind == "not_inside_program_folder":
            if _inside(program_folder, value):
                return refusal
        elif kind == "not_own_file_name":
            # "edit your own settings file" -> write_file to an invented
            # settings path outside her folder (2026-11-04, 6/6). Her own
            # files are known by name, wherever the path says they are.
            if value and _own_file_name(program_folder, value, chk.get("names", ())):
                return refusal
        elif kind == "task_has_subject":
            # "remind me thursday" -> add_task('Thursday', 'thursday'): a
            # task that is nothing but its time has no subject to remind
            # him of. time_parse decides what is a time; no word list here.
            if _only_a_time(value):
                return refusal
        elif kind == "unambiguous_time":
            # time_parse already knows when a phrase needs asking about
            # ("Thursday" said on a Thursday morning); surface its question
            # instead of letting the store guess next week.
            question = _time_question(value)
            if question:
                return refusal.replace("{detail}", question)
        elif kind == "verified_document_path":
            # Only a document-looking argument needs verifying: "Spotify"
            # is an app and goes straight through.
            if _rx(chk["document_like"]).search(value.strip().strip('"')):
                if verified_path is None or not verified_path(value):
                    return refusal
        elif kind == "known_skill":
            if known_skill is not None and not known_skill(value):
                return refusal
        else:
            logging.warning("tool policy: unknown check kind %r on %s", kind, name)
    return None


def looks_like_document(name: str, value: str) -> bool:
    """Whether `value` would need a verified path for this tool's check."""
    for chk in tool(name).get("checks", []):
        if chk["kind"] == "verified_document_path":
            return bool(_rx(chk["document_like"]).search((value or "").strip().strip('"')))
    return bool(ntpath.isabs(value or ""))


def status() -> dict:
    data = load()
    return {"file": path(), "tools": len(data["tools"]), "groups": sorted(data["groups"]),
            "mutating": sorted(mutating())}
