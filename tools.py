"""
Local system/task tools Denisse can call.

Each function here corresponds to a tool definition in brain.py's TOOLS list.
Keep these narrow and predictable — the LLM decides *when* to call them,
but the actual side effects should stay easy to reason about and safe
to run unattended.
"""

import ast
import datetime
import json
import logging
import math
import os
import re
import subprocess
import platform
import threading

import news
import obsidian_sync
import optimize
import safe_json
import spotify_control
import tasks
import timers
import vault_search
import brain_rules
import desktop_context

import json_config

# Limits, folders, word lists and result wording: tools.json, checked
# against tools.defaults.json (AGENTS.md Rule 0). The strings brain.py,
# brain.json and multistep.py read back stay here -- see that file's _doc.
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(ROOT, "tools.json")
DEFAULTS_FILE = os.path.join(ROOT, "tools.defaults.json")

log = logging.getLogger("denisse.tools")
_lock = threading.Lock()
_cache = None


def config() -> dict:
    """tools.json, checked; loaded once per process (reload() re-reads)."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = json_config.load("tools.json", CONFIG_FILE, DEFAULTS_FILE, _extra_checks)
        return _cache


def reload() -> dict:
    global _cache, _REMIND_ME
    with _lock:
        _cache = None
    _REMIND_ME = None
    return config()


def _extra_checks(data, defaults, checker):
    framing = data["tasks"]["framing"]
    if not [p for p in framing["asks"] if p.strip()]:
        framing["asks"] = checker.bad("tasks.framing.asks", "is empty", defaults["tasks"]["framing"]["asks"])
    return data


def _say(section: str, key: str, /, **fields) -> str:
    """A tools.json template filled in; one that won't format with these
    fields (a format spec for the wrong type) logs and uses the default."""
    template = config()[section][key]
    try:
        return template.format(**fields)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        log.warning("tools.json %s.%s won't format (%s) -- using the default", section, key, exc)
        with open(DEFAULTS_FILE, encoding="utf-8") as f:
            return json.load(f)[section][key].format(**fields)


def _error(section: str, key: str, /, **fields) -> str:
    # "Error" starts every failure: multistep.py and learning.json read it.
    return "Error: " + _say(section, key, **fields)


def _repo_path(key: str) -> str:
    return os.path.join(ROOT, *config()["files"][key].replace("\\", "/").split("/"))


FACTS_FILE = _repo_path("facts")
SKILLS_DIR = _repo_path("skills")


def run_shell_command(command: str) -> str:
    """Run a shell command and return combined stdout/stderr (truncated)."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config()["shell"]["timeout_seconds"],
        )
        output = (result.stdout or "") + (result.stderr or "")
        # "(no output)" is brain.json evidence.empty_results.
        return output.strip()[:config()["shell"]["max_output_chars"]] or "(no output)"
    except subprocess.TimeoutExpired:
        return _error("shell", "timed_out", seconds=config()["shell"]["timeout_seconds"])
    except Exception as e:
        return f"Error: {e}"


def _open_windows_store_app(app_name: str) -> bool:
    """Fuzzy-match app_name against installed Start Menu apps and launch it.
    Handles Microsoft Store-packaged apps (like the new Outlook) that don't
    have a plain .exe to launch via os.startfile."""
    safe_name = app_name.replace("'", "")
    ps_script = (
        f"(Get-StartApps | Where-Object {{ $_.Name -like '*{safe_name}*' }} "
        "| Select-Object -First 1 -ExpandProperty AppID)"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=config()["open_application"]["store_search_timeout_seconds"],
    )
    app_id = result.stdout.strip()
    if not app_id:
        return False
    subprocess.run(["explorer.exe", f"shell:AppsFolder\\{app_id}"], check=False)
    return True


def open_application(app_name: str) -> str:
    """Launch an installed APPLICATION by name -- Chrome, Word, Spotify,
    Fusion. Not for documents: "open a file called passport" is a file,
    so find_files it first and open the path that comes back. Asked to
    open something that isn't an app you know, search for it before
    deciding it doesn't exist (2026-10-26: this was called with
    'passport', found no such app, and Denisse told Chuen the file
    wasn't on his machine -- it was, six copies of it).
    """
    system = platform.system()
    if system != "Windows":
        try:
            if system == "Darwin":
                subprocess.run(["open", "-a", app_name], check=True)
            else:
                subprocess.run([app_name], check=True)
            return _say("open_application", "opened", app_name=app_name)
        except Exception as e:
            return "Error " + _say("open_application", "failed", app_name=app_name, error=e)

    # Windows: try a direct launch first (classic .exe apps, or anything
    # with an "App Paths" registry entry).
    try:
        os.startfile(app_name)  # type: ignore[attr-defined]
        return _say("open_application", "opened", app_name=app_name)
    except OSError:
        pass

    # Fall back to a Start Menu app search (handles Store-packaged apps).
    try:
        if _open_windows_store_app(app_name):
            return _say("open_application", "opened", app_name=app_name)
    except Exception as e:
        return "Error " + _say("open_application", "failed", app_name=app_name, error=e)

    # Point at the next step rather than stopping here. A bare "couldn't
    # find it" reads to the model as a settled answer, and it relayed that
    # to Chuen as the file not existing.
    return _say("open_application", "not_found", app_name=app_name)


def list_directory(path: str = ".") -> str:
    """List files and folders at a given path."""
    try:
        entries = os.listdir(path)
        return "\n".join(entries) if entries else _say("list_directory", "empty")
    except Exception as e:
        return f"Error: {e}"


# Folders searched by find_files when no specific location is given --
# the places a person actually keeps things, not the whole disk.
def _default_search_dirs() -> dict:
    home = os.path.expanduser("~")
    cfg = config()["find_files"]
    dirs = {name.lower(): os.path.join(home, name) for name in cfg["search_folders"]}
    onedrive_prefix = cfg["onedrive_prefix"].lower()
    # OneDrive is where a lot of real work actually lives on this machine
    # (the Obsidian vault included) -- fold in its top-level folders too,
    # if it's there.
    #
    # These must NOT be setdefault'd under their own names. Windows
    # redirects Desktop and Documents into OneDrive, which leaves a
    # near-empty C:\Users\<me>\Documents behind -- and because that stub
    # exists, setdefault kept it and silently dropped the real
    # OneDrive\Documents. On 2026-10-26 Denisse told Chuen his passport
    # wasn't on the machine; it was sitting in OneDrive\Documents\Resume
    # \IUMC, in a folder she had never been given to search. So a
    # collision registers the OneDrive copy under its own key instead,
    # and both get searched.
    for entry in sorted(os.listdir(home)) if os.path.isdir(home) else []:
        if entry.lower().startswith(onedrive_prefix):
            onedrive = os.path.join(home, entry)
            if os.path.isdir(onedrive):
                try:
                    for sub in os.listdir(onedrive):
                        sub_path = os.path.join(onedrive, sub)
                        if not os.path.isdir(sub_path) or sub.startswith("."):
                            continue
                        key = sub.lower()
                        if os.path.normcase(dirs.get(key, "")) == os.path.normcase(sub_path):
                            continue
                        if key in dirs:
                            key = f"{onedrive_prefix} {key}"
                            n = 2
                            while key in dirs:
                                key, n = f"{onedrive_prefix} {sub.lower()} {n}", n + 1
                        dirs[key] = sub_path
                except OSError:
                    pass
    return {name: path for name, path in dirs.items() if os.path.isdir(path)}


def find_files(query: str, location: str = "") -> str:
    """Search for a file by name -- use this whenever asked to find,
    locate, or search for something instead of guessing a path or
    blindly listing directories one at a time. Matches filenames
    containing `query` (case-insensitive, or a wildcard pattern like
    '*.pdf'), searched recursively through the user's common folders
    (Desktop, Documents, Downloads, Pictures, Videos, Music, GitHub, and
    any OneDrive folders present). Returns up to 25 matches with full
    paths, newest-modified first.

    location: optional, narrows the search to one folder by name (e.g.
    'downloads', 'desktop') instead of all of them -- also accepts a
    full path directly. Leave empty to search everywhere Denisse knows
    to look.
    """
    import fnmatch

    cfg = config()["find_files"]
    query = (query or "").strip()
    if not query:
        return _error("find_files", "empty_query")

    search_dirs = _default_search_dirs()
    prefix = cfg["onedrive_prefix"].lower()
    if location:
        loc_key = location.strip().lower()
        # "documents" has to reach "onedrive documents" too, or narrowing
        # the search puts you back in the empty redirected stub.
        named = {k: v for k, v in search_dirs.items()
                 if k == loc_key or k == f"{prefix} {loc_key}"
                 or k.startswith(f"{prefix} {loc_key} ")}
        if named:
            roots = named
        elif os.path.isdir(location):
            roots = {location: location}
        else:
            known = ", ".join(sorted(search_dirs)) if search_dirs else cfg["no_known_folders"]
            return _say("find_files", "unknown_location", location=location, known=known)
    else:
        roots = search_dirs

    if not roots:
        return _error("find_files", "no_folders")

    is_pattern = any(ch in query for ch in "*?[")
    query_lower = query.lower()

    # Natural-language queries (the model tends to pass whatever the user
    # said, like "my japan flight pdf") won't literally appear as one
    # contiguous substring in a real filename like "Japan_flight.pdf" --
    # normalize both sides (split on anything that isn't a letter/digit)
    # and require every meaningful query word to show up, in any order,
    # rather than an exact phrase match.
    filler = set(cfg["filler_words"])
    skip_dirs = {d.lower() for d in cfg["skip_dir_names"]}
    max_scanned, max_results = cfg["max_files_scanned"], cfg["max_results"]

    def _tokens(s: str) -> list:
        return [w for w in re.split(r"[^a-z0-9]+", s.lower()) if w]

    query_tokens = [t for t in _tokens(query) if t not in filler] or _tokens(query)

    matches = []
    scanned = 0
    for root_name, root_path in roots.items():
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d.lower() not in skip_dirs
            ]
            for fname in filenames:
                scanned += 1
                if scanned > max_scanned:
                    break
                if is_pattern:
                    name_matches = fnmatch.fnmatch(fname.lower(), query_lower)
                else:
                    fname_normalized = " ".join(_tokens(fname))
                    name_matches = query_lower in fname.lower() or all(
                        tok in fname_normalized for tok in query_tokens
                    )
                if name_matches:
                    full_path = os.path.join(dirpath, fname)
                    try:
                        mtime = os.path.getmtime(full_path)
                    except OSError:
                        mtime = 0
                    matches.append((mtime, full_path))
            if scanned > max_scanned:
                break
        if scanned > max_scanned:
            break

    # brain.py and brain.json read these two lines back ("Found 1 match",
    # "No files matching ... found in"), so they stay fixed here.
    if not matches:
        return f"No files matching '{query}' found in {', '.join(roots)}."

    matches.sort(reverse=True)
    shown = matches[:max_results]
    lines = [f"Found {len(matches)} match(es), showing {len(shown)} (newest first):"]
    lines += [path for _, path in shown]
    if len(matches) > max_results:
        lines.append(_say("find_files", "more", more=len(matches) - max_results))
    return "\n".join(lines)


def read_file(path: str, max_chars: int = 0) -> str:
    """Read a text file's contents (truncated to max_chars, else tools.json
    read_file.max_chars). "(empty file)" is read by multistep.py."""
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        max_chars = config()["read_file"]["max_chars"]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars)
        return content or "(empty file)"
    except Exception as e:
        return f"Error: {e}"


def write_file(path: str, content: str) -> str:
    """Write text content to a file, overwriting it if it exists."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return _say("write_file", "wrote", chars=len(content), path=path)
    except Exception as e:
        return f"Error: {e}"


_MATH_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "round": round,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log, "log10": math.log10, "log2": math.log2,
    "exp": math.exp, "floor": math.floor, "ceil": math.ceil,
    "factorial": math.factorial, "pow": pow, "min": min, "max": max,
}
_MATH_NAMES = {"pi": math.pi, "e": math.e, "tau": math.tau}

# Small local models are unreliable at exact arithmetic done "in their head"
# (get multi-digit multiplication, percentages, and order of operations
# wrong with real confidence) -- this evaluates the actual expression in
# code instead, the same fix already applied to time/weather rather than
# trusting the model to compute or fetch those itself. Uses ast, not
# eval(), so it can only ever be arithmetic -- no arbitrary code execution
# even if a prompt injection tried to smuggle something through here.
def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"unsupported constant: {node.value!r}")
    if isinstance(node, ast.BinOp):
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        ops = {
            ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
            ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
            ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
            ast.Pow: lambda a, b: a ** b,
        }
        if type(node.op) not in ops:
            raise ValueError(f"unsupported operator: {type(node.op).__name__}")
        return ops[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp):
        val = _safe_eval(node.operand)
        if isinstance(node.op, ast.USub):
            return -val
        if isinstance(node.op, ast.UAdd):
            return val
        raise ValueError(f"unsupported unary operator: {type(node.op).__name__}")
    if isinstance(node, ast.Name):
        if node.id in _MATH_NAMES:
            return _MATH_NAMES[node.id]
        raise ValueError(f"unknown name: {node.id}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _MATH_FUNCS:
            raise ValueError("unsupported function call")
        args = [_safe_eval(a) for a in node.args]
        return _MATH_FUNCS[node.func.id](*args)
    raise ValueError(f"unsupported expression: {type(node).__name__}")


def calculate(expression: str) -> str:
    """Evaluate a math expression exactly and return the result. Supports
    + - * / // % ** parentheses, unary minus, constants pi/e, and
    functions sqrt/abs/round/sin/cos/tan/log/log10/log2/exp/floor/ceil/
    factorial/pow/min/max (angles in radians). Always use this for any
    arithmetic rather than computing it yourself, no matter how simple it
    looks -- multi-digit multiplication and percentages are exactly where
    doing it "in your head" goes wrong."""
    try:
        tree = ast.parse(expression, mode="eval")
        result = _safe_eval(tree.body if hasattr(tree, "body") else tree)
    except ZeroDivisionError:
        return _error("calculate", "division_by_zero")
    except Exception as e:
        return _error("calculate", "failed", expression=expression, error=e)
    if isinstance(result, float):
        # Binary floating point can't represent most decimals exactly
        # (9.9 - 9.11 comes out 0.7900000000000009), and replies get read
        # aloud verbatim -- round off the representation noise before it
        # ever reaches the model or the user, while keeping real precision.
        result = round(result, config()["calculate"]["round_digits"])
        if result.is_integer():
            result = int(result)
    return f"{expression} = {result}"


def get_current_time(timezone: str = "") -> str:
    """Return the current date and time. With no timezone, this machine's
    own local time. With an IANA timezone name (e.g. 'Asia/Tokyo'), the
    current time there instead -- entirely offline (the 'tzdata' package
    ships the timezone database; no lookup service involved).

    Includes a ready-made 12-hour ('7:25 PM') string alongside the 24-hour
    one -- caught the model mis-converting 24-hour time by hand when
    speaking it naturally (said "7:00 PM" for 19:25, i.e. got both the
    minutes and the wording wrong), so this does the conversion in code
    instead of leaving arithmetic to the model."""
    fmt = config()["time"]
    if not timezone:
        now = datetime.datetime.now()
        tz_suffix = ""
    else:
        try:
            from zoneinfo import ZoneInfo
            now = datetime.datetime.now(ZoneInfo(timezone))
            tz_suffix = " " + now.strftime(fmt["zone_format"])
        except Exception as e:
            return _error("time", "unknown_timezone", timezone=timezone, error=e)
    twelve_hour = now.strftime(fmt["twelve_hour_format"]).lstrip("0")
    return _say("time", "reply", datetime=now.strftime(fmt["datetime_format"]),
                zone=tz_suffix, twelve_hour=twelve_hour)


def get_weather(location: str) -> str:
    """Get a short current-weather summary for a place, via wttr.in's free
    API (no key needed). A single clean line -- not the ANSI-art terminal
    report `curl wttr.in` gives, which isn't useful as text and has
    previously caused confused/hallucinated replies when used via
    run_shell_command."""
    import urllib.parse
    import urllib.request

    cfg = config()["weather"]
    try:
        url = _say("weather", "url", location=urllib.parse.quote(location), format=cfg["format"])
        with urllib.request.urlopen(url, timeout=cfg["timeout_seconds"]) as resp:
            return resp.read().decode("utf-8", errors="replace").strip()
    except Exception as e:
        return _error("weather", "failed", location=location, error=e)


def get_news(topic: str = "") -> str:
    """Get the latest real headlines from CNA (Channel NewsAsia), via
    their own public RSS feed -- no API key. Give an optional topic (e.g.
    'business', 'singapore', 'world') to filter; omit for general
    top headlines. Summarize these in your own words when replying --
    don't just read the list back verbatim.

    Returns one flowing sentence, not a bulleted list -- a multi-line
    bulleted result here was empirically much less reliable (the model
    would sometimes claim it found no news, or answer something
    unrelated, despite real headlines being right there in the tool
    result) than plain prose, which every other tool here already uses."""
    cfg = config()["news"]
    try:
        headlines = news.fetch_headlines(topic=topic, count=cfg["count"])
    except Exception as e:
        return _error("news", "failed", error=e)
    if not headlines:
        return _say("news", "none", topic=_say("news", "none_topic", topic=topic) if topic else "")
    return _say("news", "headlines", headlines=cfg["separator"].join(headlines))


def get_voice_settings() -> str:
    """Check what actually speaks your replies out loud right now -- the
    text-only model has no built-in knowledge of this, so if asked about
    your own voice, call this instead of guessing."""
    import voice_output

    if not os.environ.get("INWORLD_API_KEY"):
        return _say("voice", "no_key")
    return _say("voice", "neural", voice=voice_output.INWORLD_VOICE, model=voice_output.INWORLD_MODEL)


def get_brain_location() -> str:
    """Report the real filesystem path(s) where your own code, memory, and
    Obsidian-synced state actually live -- call this instead of guessing a
    path yourself (paths with spaces/apostrophes, like the vault's, are
    easy to mangle when typed out by hand)."""
    import obsidian_sync

    code_dir = os.path.dirname(os.path.abspath(__file__))
    import instincts
    rules_status = brain_rules.status()
    rules = brain_rules.load()
    learned = instincts.status()
    vault_on = rules.vault_search and os.environ.get("DENISSE_VAULT_SEARCH", "1").lower() not in ("0", "false", "off", "no")
    w = config()["brain_location"]

    def say(key, /, **f):
        return _say("brain_location", key, **f)

    def on_off(flag):
        return w["on"] if flag else w["off"]

    # A lesson needs this many positives per negative: 4 -> 80% positive.
    per_negative = instincts.CONFIG["activation"]["min_positives_per_negative"]
    positive_percent = round(100 * per_negative / (per_negative + 1))
    save = w["save_explicit"] if rules.save_facts == "explicit" else w["save_auto"]
    lines = [say("saving", save=save, turns=rules.recent_turns),
             say("vault", state=on_off(vault_on), chars=rules.vault_max_chars),
             say("learning", state=on_off(learned["enabled"]), confirmed=learned["confirmed_lessons"],
                 candidates=learned["candidates"], confirmations=rules.learning_confirmations,
                 positive_percent=positive_percent),
             say("learning_limits"),
             say("preferences", file=rules_status["file"]),
             say("places", code=code_dir, memory=os.path.dirname(FACTS_FILE))]
    if rules_status["error"]:
        lines.append(say("invalid_settings", error=rules_status["error"]))
    if obsidian_sync._vault_available():
        lines.append(say("mirrored", vault=obsidian_sync.BRAIN_DIR))
    else:
        lines.append(say("no_vault"))
    return "\n".join(lines)


def clean_temp_files() -> str:
    """Delete old (24h+ untouched) files from the user's Temp folder to
    free disk space. Already runs automatically in the background every
    hour (see optimize.py) -- this lets it be triggered on demand too."""
    removed = optimize.clean_temp_files()
    if not removed:
        return _say("clean_temp", "nothing")
    freed_mb = sum(f["size"] for f in removed) / 1024**2
    return _say("clean_temp", "removed", count=len(removed), mb=freed_mb)


def _load_facts() -> dict:
    """The stored facts; {} if none. A file that won't parse is copied
    aside by safe_json, so remember() can't save over everything else."""
    return safe_json.read_json(FACTS_FILE, {})


def _save_facts(facts: dict) -> None:
    safe_json.write_json_atomic(FACTS_FILE, facts, indent=2)
    obsidian_sync.sync_facts(facts)


def remember(key: str, value: str) -> str:
    """Save a fact about the user for later recall (e.g. key='favorite_coffee', value='flat white')."""
    with safe_json.locked(FACTS_FILE):
        facts = _load_facts()
        facts[key] = value
        _save_facts(facts)
    return _say("facts", "remembered", key=key, value=value)


# recall() used to be an exact dict lookup, so the model had to guess the
# key it (or an earlier session) chose when saving: "favourite_editor",
# "code_editor" and "editor" all missed a fact stored as favorite_editor.
# Facts number in the dozens at most, so a lexical match over keys AND
# values is enough -- no embedding model to download, nothing to keep in
# sync. Spelling variants are folded first, then each fact is scored on
# shared words (a shared prefix counts, so editor/editing match). The lists
# and weights are tools.json "facts".
def _fact_words(text: str) -> set:
    cfg = config()["facts"]
    spelling = {pair[0]: pair[1] for pair in cfg["spelling"] if len(pair) == 2}
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {spelling.get(w, w) for w in words} - set(cfg["ignore_words"])


def _word_overlap(query: set, words: set) -> float:
    cfg = config()["facts"]
    n = cfg["prefix_length"]
    score = 0.0
    for q in query:
        if q in words:
            score += 1.0
        elif len(q) >= n and any(w[:n] == q[:n] for w in words if len(w) >= n):
            score += cfg["prefix_score"]
    return score


def recall(key: str) -> str:
    """Recall a previously remembered fact, tolerating a key that's worded
    or spelled differently from the one it was saved under."""
    facts = _load_facts()
    if key in facts:
        return facts[key]
    # "I don't have anything remembered" is brain.json
    # evidence.found_nothing_patterns, so it stays here.
    if not facts:
        return "I don't have anything remembered yet."

    cfg = config()["facts"]
    query = _fact_words(key)
    scored = []
    for k, v in facts.items():
        # The key is what the fact is ABOUT, so it counts double; the value
        # still helps ("python" finds favorite_programming_language).
        s = cfg["key_weight"] * _word_overlap(query, _fact_words(k)) + _word_overlap(query, _fact_words(str(v)))
        if s > 0:
            scored.append((s, k, v))
    scored.sort(key=lambda t: -t[0])

    if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        _, k, v = scored[0]
        return _say("facts", "match", key=k, value=v)
    if scored:
        top = "\n".join(_say("facts", "match", key=k, value=v) for _, k, v in scored[:cfg["closest_shown"]])
        return _say("facts", "closest", key=key, matches=top)
    known = ", ".join(list(facts)[:cfg["known_shown"]])
    return _say("facts", "not_found", key=key, known=known)


def list_known_facts() -> str:
    """List every fact currently remembered about the user."""
    facts = _load_facts()
    if not facts:
        return "I don't have anything remembered yet."
    return "\n".join(_say("facts", "match", key=k, value=v) for k, v in facts.items())


def _skill_description(path: str) -> str:
    """Pull the 'description:' line out of a skill file's frontmatter."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("description:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def list_skills() -> str:
    """List available skills -- specific how-to guides for tasks Denisse
    has been taught a procedure for. Read one with read_skill before
    attempting a task it covers."""
    if not os.path.isdir(SKILLS_DIR):
        return _say("skills", "none")
    names = sorted(f[:-3] for f in os.listdir(SKILLS_DIR) if f.endswith(".md"))
    if not names:
        return _say("skills", "none")
    lines = []
    for name in names:
        desc = _skill_description(os.path.join(SKILLS_DIR, f"{name}.md"))
        lines.append(f"{name}: {desc}" if desc else name)
    return "\n".join(lines)


def read_skill(name: str) -> str:
    """Read the full instructions for one skill by name (see list_skills)."""
    safe_name = os.path.basename(name).removesuffix(".md")
    path = os.path.join(SKILLS_DIR, f"{safe_name}.md")
    if not os.path.isfile(path):
        return _say("skills", "unknown", name=name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# Built from tools.json tasks.framing: each slot is "one of these phrases,
# then spacing", optional except asks. Longest phrase first, so "there's an"
# beats "there's".
_REMIND_ME = None


def _framing_pattern():
    global _REMIND_ME
    if _REMIND_ME is None:
        framing = config()["tasks"]["framing"]

        def slot(name, optional=True):
            phrases = sorted({p.strip() for p in framing[name] if p.strip()}, key=len, reverse=True)
            if not phrases:
                return ""
            alt = "|".join(r"\s+".join(map(re.escape, p.split())) for p in phrases)
            return f"(?:(?:{alt})\\s+)" + ("?" if optional else "")

        _REMIND_ME = re.compile("^" + slot("polite") + slot("asks", optional=False) + slot("addressee")
                                + slot("links") + slot("lead_ins"), re.IGNORECASE)
    return _REMIND_ME


# Registry mapping tool name -> callable, used by brain.py's tool-use loop.
def add_task(text: str, due: str = "") -> str:
    """Add something Chuen has to do, with an optional deadline in his own
    words. Returns confirmation including how the deadline was understood,
    so a misread date is visible immediately rather than at the deadline.

    time_parse turns his words into the time, not the model. A phrase that
    could mean two days ("Thursday at 3" said on Thursday morning) adds
    nothing and hands the model the question to ask him instead."""
    import time_parse

    if due and due.strip():
        parsed = time_parse.parse(due)
        if parsed.question:
            return _say("tasks", "ambiguous_due", due=due.strip(), question=parsed.question)
    # The model copies his framing into the task: "remind me there is a
    # robotics meeting" (2026-11-03). The task is the meeting. This trims
    # the model's argument, not his words.
    text = _framing_pattern().sub("", (text or "").strip(), count=1).strip() or (text or "").strip()
    t = tasks.add_task(text, due)
    if t["due"]:
        # In words ("Thursday 5 Nov, 15:00"), so a wrong parse is visible.
        when = datetime.datetime.fromisoformat(t["due"])
        # brain.py _task_sentence parses these three lines back: fixed here.
        return f"Added: {t['text']} -- due {time_parse.describe(when)}."
    if t["due_text"]:
        return (f"Added: {t['text']} -- but I couldn't work out a date from "
                f"\"{t['due_text']}\", so it has no deadline.")
    return f"Added: {t['text']} (no deadline)"


def list_open_tasks() -> str:
    """The open tasks, and where they're kept.

    The location is included because "where is the task list" is a question
    Chuen actually asks, and without a true answer in front of it the model
    invents one -- it confidently reported a nonexistent
    'C:\\Users\\Xichu\\GitHub\\denisse\\tasks.txt' two times out of three
    (2026-09-25). Telling it the real path beats telling it not to guess.
    """
    where = _say("tasks", "where", file=tasks.TASKS_FILE)
    # Pick up anything ticked in the Obsidian note before answering, so the
    # two views never disagree in front of him.
    ticked = tasks.pull_vault_ticks()
    open_tasks = tasks.list_tasks()
    if ticked:
        names = ", ".join(t["text"] for t in ticked)
        where = _say("tasks", "ticked", count=len(ticked), names=names) + where
    if not open_tasks:
        return _say("tasks", "empty", where=where)
    import reminders_config
    overdue, soon = tasks.due_within(reminders_config.get("tasks.due_soon_hours"))
    over_ids = {t["id"] for t in overdue}
    lines = []
    for t in open_tasks:
        mark = config()["tasks"]["overdue_mark"] if t["id"] in over_ids else ""
        lines.append(f"- {mark}{tasks.describe(t)}")
    return _say("tasks", "open", count=len(open_tasks), lines="\n".join(lines), where=where)


def complete_task(task: str) -> str:
    """Mark a task done. Matched on words, and refuses when ambiguous.

    Says how many are left but deliberately NOT which. An earlier version
    named them -- "1 still open: finish the ViTrox application" -- and the
    model read that as an instruction and completed that task too, marking
    work done that Chuen had never mentioned. Naming a task in the result of
    a completion hands a small model a target. Measured 2026-09-23: three
    runs out of three closed both tasks.
    """
    t = tasks.complete_task(task)
    if t is None:
        return _say("tasks", "no_match", task=task)
    remaining = len(tasks.list_tasks())
    if not remaining:
        return f"Marked done: {t['text']}. That was the last one."
    return (f"Marked done: {t['text']}. {remaining} other "
            f"task{'s' if remaining != 1 else ''} still open; "
            "use list_tasks only if he asks what's left.")


def remove_task(task: str) -> str:
    """Delete a task outright -- for things added by mistake."""
    t = tasks.remove_task(task)
    if t is None:
        return _say("tasks", "remove_no_match", task=task)
    return f"Removed: {t['text']}"


# study_mode imports Tk and PIL for its bubble and badge; importing it here
# at module level would drag a window toolkit into every headless user of
# tools.py. The clock functions are looked up when called.
def study_session_status() -> str:
    import study_mode
    return study_mode.session_status()


def start_study_session() -> str:
    import study_mode
    return study_mode.start_session()


def pause_study_session() -> str:
    import study_mode
    return study_mode.pause_session()


def resume_study_session() -> str:
    import study_mode
    return study_mode.resume_session()


def stop_study_session() -> str:
    import study_mode
    return study_mode.stop_session()


def reply_preference(action: str = "add", text: str = "") -> str:
    import reply_preferences
    return reply_preferences.tool(action, text)


# The study tutor (tutor/chat.py, 2026-11-05): looked up when called, so a
# headless user of tools.py never loads the tutor's config or the vault.
def tutor_explain(topic: str, module: str = "") -> str:
    from tutor import chat
    return chat.explain(topic, module)


def tutor_quiz(module: str = "", topic: str = "", count: int = 0) -> str:
    from tutor import chat
    return chat.quiz(module, topic, count)


def tutor_weak_topics(module: str = "") -> str:
    from tutor import chat
    return chat.weak_topics(module)


def tutor_exam_question(module: str = "", topic: str = "") -> str:
    from tutor import chat
    return chat.exam_question(module, topic)


def tutor_mark_answer(answer: str, module: str = "") -> str:
    from tutor import chat
    return chat.mark_answer(answer, module)


def tutor_consult(question: str, module: str = "") -> str:
    """The tutor's opinion on a study question (part 5): grounded in his
    notes, with its confidence and sources, said as the tutor's view."""
    from tutor import chat
    return chat.consult(question, module)


def tutor_round_offer(round_no: int) -> str:
    """study_mode's end-of-round hook (not a model tool): the tutor's offer
    of a short check, or '' when it has nothing to quiz from."""
    from tutor import chat
    return chat.round_offer(round_no)


def check_calendar(day: str = "") -> str:
    """What is on his calendar -- which, today, is nothing she can see.

    No calendar is connected (config.json settings.calendar is unset; the
    task list is deliberately not a calendar, see tasks.py). The honest
    result is that, said plainly: on 2026-11-04 "what's on my calendar
    tomorrow?" got "I don't have anything scheduled for tomorrow" -- an
    empty calendar asserted with no calendar to read. A tool result is the
    one thing the model treats as ground truth, so the fact lives here.
    When a calendar is wired up, this is where it answers from."""
    import settings
    if not settings.get("calendar"):
        return _say("calendar", "not_connected", day=day or config()["calendar"]["any_day"])
    return _error("calendar", "no_reader")


def draft_email(to: str = "", subject: str = "", gist: str = "", replying_to: str = "",
                attachments=None) -> str:
    """An email DRAFT for him to send himself (email_drafts.py). Never sends."""
    import email_drafts
    return email_drafts.draft_email(to=to, subject=subject, gist=gist,
                                    replying_to=replying_to, attachments=attachments)


TOOL_FUNCTIONS = {
    "run_shell_command": run_shell_command,
    "open_application": open_application,
    "list_directory": list_directory,
    "find_files": find_files,
    "read_file": read_file,
    "write_file": write_file,
    "get_current_time": get_current_time,
    "calculate": calculate,
    "get_weather": get_weather,
    "get_news": get_news,
    "get_voice_settings": get_voice_settings,
    "get_brain_location": get_brain_location,
    "get_system_status": optimize.get_system_status,
    "clean_temp_files": clean_temp_files,
    "list_startup_programs": optimize.list_startup_programs,
    "disable_startup_program": optimize.disable_startup_program,
    "restore_startup_program": optimize.restore_startup_program,
    "remember": remember,
    "recall": recall,
    "list_known_facts": list_known_facts,
    "list_skills": list_skills,
    "read_skill": read_skill,
    "add_task": add_task,
    "list_tasks": list_open_tasks,
    "complete_task": complete_task,
    "remove_task": remove_task,
    "search_vault": vault_search.search_vault,
    "play_song": spotify_control.play_song,
    "pause_music": spotify_control.pause_music,
    "resume_music": spotify_control.resume_music,
    "skip_song": spotify_control.skip_song,
    "previous_song": spotify_control.previous_song,
    "current_track": spotify_control.current_track,
    "set_timer": timers.set_timer,
    "cancel_timer": timers.cancel_timer,
    "list_timers": timers.list_timers,
    "look_at_this": desktop_context.look_at_this,
    # Study mode (study_mode.py), model-visible since 2026-11-03: before,
    # it only ran when a whole-message phrase matched in tray.py, so "is
    # the study session running?" and "pause sutuy timer" reached a model
    # that had no idea it existed.
    "study_session_status": study_session_status,
    "start_study_session": start_study_session,
    "pause_study_session": pause_study_session,
    "resume_study_session": resume_study_session,
    "stop_study_session": stop_study_session,
    "reply_preference": reply_preference,
    "check_calendar": check_calendar,
    # The study tutor (tutor/chat.py), model-visible since 2026-11-05.
    "tutor_explain": tutor_explain,
    "tutor_quiz": tutor_quiz,
    "tutor_weak_topics": tutor_weak_topics,
    "tutor_exam_question": tutor_exam_question,
    "tutor_mark_answer": tutor_mark_answer,
    "tutor_consult": tutor_consult,
    # Email drafts (email_drafts.py, 2026-11-07): saved, never sent.
    "draft_email": draft_email,
}
