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
import math
import os
import re
import subprocess
import platform

import news
import obsidian_sync
import optimize
import spotify_control
import tasks
import timers
import vault_search
import brain_rules

FACTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "memory", "facts.json"
)
SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "skills"
)


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
            timeout=30,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return output.strip()[:4000] or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 30s"
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
        timeout=15,
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
            return f"Opened {app_name}"
        except Exception as e:
            return f"Error opening {app_name}: {e}"

    # Windows: try a direct launch first (classic .exe apps, or anything
    # with an "App Paths" registry entry).
    try:
        os.startfile(app_name)  # type: ignore[attr-defined]
        return f"Opened {app_name}"
    except OSError:
        pass

    # Fall back to a Start Menu app search (handles Store-packaged apps).
    try:
        if _open_windows_store_app(app_name):
            return f"Opened {app_name}"
    except Exception as e:
        return f"Error opening {app_name}: {e}"

    # Point at the next step rather than stopping here. A bare "couldn't
    # find it" reads to the model as a settled answer, and it relayed that
    # to Chuen as the file not existing.
    return (f"No application called '{app_name}' (tried a direct launch and "
            f"a Start Menu search). This only looks at installed apps, not "
            f"at files. If '{app_name}' is a document, call "
            f"find_files(query='{app_name}') before telling Chuen anything "
            f"about whether it exists.")


def list_directory(path: str = ".") -> str:
    """List files and folders at a given path."""
    try:
        entries = os.listdir(path)
        return "\n".join(entries) if entries else "(empty directory)"
    except Exception as e:
        return f"Error: {e}"


# Folders searched by find_files when no specific location is given --
# the places a person actually keeps things, not the whole disk.
def _default_search_dirs() -> dict:
    home = os.path.expanduser("~")
    dirs = {
        "desktop": os.path.join(home, "Desktop"),
        "documents": os.path.join(home, "Documents"),
        "downloads": os.path.join(home, "Downloads"),
        "pictures": os.path.join(home, "Pictures"),
        "videos": os.path.join(home, "Videos"),
        "music": os.path.join(home, "Music"),
        "github": os.path.join(home, "GitHub"),
    }
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
        if entry.lower().startswith("onedrive"):
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
                            key = f"onedrive {key}"
                            n = 2
                            while key in dirs:
                                key, n = f"onedrive {sub.lower()} {n}", n + 1
                        dirs[key] = sub_path
                except OSError:
                    pass
    return {name: path for name, path in dirs.items() if os.path.isdir(path)}


# Directory names to never descend into -- noisy, huge, or not where a
# person keeps things they'd ask Denisse to find.
_SKIP_DIR_NAMES = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".cache",
    "$recycle.bin", "system volume information", ".mcp-auth",
    ".claude-server-commander",
}
_MAX_FILES_SCANNED = 40000
_MAX_RESULTS = 25


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

    query = (query or "").strip()
    if not query:
        return "Error: give me something to search for."

    search_dirs = _default_search_dirs()
    if location:
        loc_key = location.strip().lower()
        # "documents" has to reach "onedrive documents" too, or narrowing
        # the search puts you back in the empty redirected stub.
        named = {k: v for k, v in search_dirs.items()
                 if k == loc_key or k == f"onedrive {loc_key}"
                 or k.startswith(f"onedrive {loc_key} ")}
        if named:
            roots = named
        elif os.path.isdir(location):
            roots = {location: location}
        else:
            known = ", ".join(sorted(search_dirs)) if search_dirs else "none found"
            return f"Don't know a location called '{location}'. Known folders: {known}."
    else:
        roots = search_dirs

    if not roots:
        return "Error: couldn't find any of the usual folders to search."

    is_pattern = any(ch in query for ch in "*?[")
    query_lower = query.lower()

    # Natural-language queries (the model tends to pass whatever the user
    # said, like "my japan flight pdf") won't literally appear as one
    # contiguous substring in a real filename like "Japan_flight.pdf" --
    # normalize both sides (split on anything that isn't a letter/digit)
    # and require every meaningful query word to show up, in any order,
    # rather than an exact phrase match.
    _STOPWORDS = {
        "my", "the", "a", "an", "for", "me", "please", "file", "files",
        "find", "locate", "search", "of", "that", "this",
    }
    import re

    def _tokens(s: str) -> list:
        return [w for w in re.split(r"[^a-z0-9]+", s.lower()) if w]

    query_tokens = [t for t in _tokens(query) if t not in _STOPWORDS] or _tokens(query)

    matches = []
    scanned = 0
    for root_name, root_path in roots.items():
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d.lower() not in _SKIP_DIR_NAMES
            ]
            for fname in filenames:
                scanned += 1
                if scanned > _MAX_FILES_SCANNED:
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
            if scanned > _MAX_FILES_SCANNED:
                break
        if scanned > _MAX_FILES_SCANNED:
            break

    if not matches:
        return f"No files matching '{query}' found in {', '.join(roots)}."

    matches.sort(reverse=True)
    shown = matches[:_MAX_RESULTS]
    lines = [f"Found {len(matches)} match(es), showing {len(shown)} (newest first):"]
    lines += [path for _, path in shown]
    if len(matches) > _MAX_RESULTS:
        lines.append(f"...and {len(matches) - _MAX_RESULTS} more, narrow the query to see them.")
    return "\n".join(lines)


def read_file(path: str, max_chars: int = 4000) -> str:
    """Read a text file's contents (truncated)."""
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
        return f"Wrote {len(content)} chars to {path}"
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
        return "Error: division by zero."
    except Exception as e:
        return f"Error: couldn't evaluate '{expression}' ({e})."
    if isinstance(result, float):
        # Binary floating point can't represent most decimals exactly
        # (9.9 - 9.11 comes out 0.7900000000000009), and replies get read
        # aloud verbatim -- round off the representation noise before it
        # ever reaches the model or the user, while keeping real precision.
        result = round(result, 10)
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
    if not timezone:
        now = datetime.datetime.now()
        tz_suffix = ""
    else:
        try:
            from zoneinfo import ZoneInfo
            now = datetime.datetime.now(ZoneInfo(timezone))
            tz_suffix = " " + now.strftime("%Z")
        except Exception as e:
            return f"Error: unknown timezone '{timezone}' ({e})"
    twelve_hour = now.strftime("%I:%M %p").lstrip("0")
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')}{tz_suffix} ({twelve_hour})"


def get_weather(location: str) -> str:
    """Get a short current-weather summary for a place, via wttr.in's free
    API (no key needed). A single clean line -- not the ANSI-art terminal
    report `curl wttr.in` gives, which isn't useful as text and has
    previously caused confused/hallucinated replies when used via
    run_shell_command."""
    import urllib.parse
    import urllib.request

    try:
        fmt = "%l:+%C,+%t+(feels+like+%f),+%h+humidity,+%w+wind"
        url = f"https://wttr.in/{urllib.parse.quote(location)}?format={fmt}&m"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read().decode("utf-8", errors="replace").strip()
    except Exception as e:
        return f"Error: couldn't get weather for '{location}' ({e})"


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
    try:
        headlines = news.fetch_headlines(topic=topic, count=6)
    except Exception as e:
        return f"Error: couldn't fetch CNA news ({e})"
    if not headlines:
        return f"No current CNA headlines found{f' for {topic!r}' if topic else ''}."
    return "Today's CNA headlines: " + "; ".join(headlines) + "."


def get_voice_settings() -> str:
    """Check what actually speaks your replies out loud right now -- the
    text-only model has no built-in knowledge of this, so if asked about
    your own voice, call this instead of guessing."""
    import voice_output

    if not os.environ.get("INWORLD_API_KEY"):
        return (
            "No Inworld API key is configured, so replies fall back to the "
            "local Windows voice (pyttsx3), not a neural voice."
        )
    return (
        f"Neural voice: '{voice_output.INWORLD_VOICE}' via Inworld's "
        f"'{voice_output.INWORLD_MODEL}' model. Falls back to the local "
        "Windows voice only if that fails."
    )


def get_brain_location() -> str:
    """Report the real filesystem path(s) where your own code, memory, and
    Obsidian-synced state actually live -- call this instead of guessing a
    path yourself (paths with spaces/apostrophes, like the vault's, are
    easy to mangle when typed out by hand)."""
    import obsidian_sync

    code_dir = os.path.dirname(os.path.abspath(__file__))
    import instincts
    config = brain_rules.status()
    rules = brain_rules.load()
    learned = instincts.status()
    vault_on = rules.vault_search and os.environ.get("DENISSE_VAULT_SEARCH", "1").lower() not in ("0", "false", "off", "no")
    save = "only when asked" if rules.save_facts == "explicit" else "when useful facts appear in conversation"
    lines = [f"I save personal facts {save}. I use the last {rules.recent_turns} conversation turns.",
             f"Vault lookup is {'on' if vault_on else 'off'}, limited to {rules.vault_max_chars} characters per lookup.",
             f"Learning is {'on' if learned['enabled'] else 'off'}: {learned['confirmed_lessons']} confirmed lessons, "
             f"{learned['candidates']} candidates. Activation requires {rules.learning_confirmations} accepted fixes "
             "and at least 80% positive feedback on that lesson.",
             "Learning changes tool selection and supported read-only retrieval intent; it cannot grant new action permissions or rewrite code.",
             f"Editable preferences: {config['file']}.",
             f"Code: {code_dir}. Conversation and facts: {os.path.dirname(FACTS_FILE)}."]
    if config["error"]:
        lines.append(f"The last settings edit was invalid: {config['error']}. Keeping the last valid settings.")
    if obsidian_sync._vault_available():
        lines.append(
            f"That memory also gets mirrored (read-only, one-way) into the "
            f"Obsidian vault at {obsidian_sync.BRAIN_DIR}. The mirror is not my conversation store. "
            "Vault notes are searchable on demand when enabled; validated routing lessons are also stored there."
        )
    else:
        lines.append("The Obsidian vault isn't currently reachable, so nothing is mirrored there right now.")
    return "\n".join(lines)


def clean_temp_files() -> str:
    """Delete old (24h+ untouched) files from the user's Temp folder to
    free disk space. Already runs automatically in the background every
    hour (see optimize.py) -- this lets it be triggered on demand too."""
    removed = optimize.clean_temp_files()
    if not removed:
        return "Nothing to clean -- no temp files old enough to remove."
    freed_mb = sum(f["size"] for f in removed) / 1024**2
    return f"Removed {len(removed)} temp file(s), freeing {freed_mb:.1f} MB."


def _load_facts() -> dict:
    if not os.path.exists(FACTS_FILE):
        return {}
    try:
        with open(FACTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_facts(facts: dict) -> None:
    os.makedirs(os.path.dirname(FACTS_FILE), exist_ok=True)
    with open(FACTS_FILE, "w", encoding="utf-8") as f:
        json.dump(facts, f, indent=2)
    obsidian_sync.sync_facts(facts)


def remember(key: str, value: str) -> str:
    """Save a fact about the user for later recall (e.g. key='favorite_coffee', value='flat white')."""
    facts = _load_facts()
    facts[key] = value
    _save_facts(facts)
    return f"Got it, I'll remember that {key} = {value}."


# recall() used to be an exact dict lookup, so the model had to guess the
# key it (or an earlier session) chose when saving: "favourite_editor",
# "code_editor" and "editor" all missed a fact stored as favorite_editor.
# Facts number in the dozens at most, so a lexical match over keys AND
# values is enough -- no embedding model to download, nothing to keep in
# sync. Spelling variants are folded first, then each fact is scored on
# shared words (a shared 5-letter prefix counts, so editor/editing match).
_SPELLING = {"favourite": "favorite", "colour": "color", "organisation":
             "organization", "programme": "program", "centre": "center"}
_RECALL_STOP = {"my", "the", "a", "an", "what", "whats", "is", "are", "do",
                "does", "i", "use", "which", "of", "for", "to", "me", "you",
                "your", "about", "and", "in", "on", "s"}


def _fact_words(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {_SPELLING.get(w, w) for w in words} - _RECALL_STOP


def _word_overlap(query: set, words: set) -> float:
    score = 0.0
    for q in query:
        if q in words:
            score += 1.0
        elif len(q) >= 5 and any(w[:5] == q[:5] for w in words if len(w) >= 5):
            score += 0.6
    return score


def recall(key: str) -> str:
    """Recall a previously remembered fact, tolerating a key that's worded
    or spelled differently from the one it was saved under."""
    facts = _load_facts()
    if key in facts:
        return facts[key]
    if not facts:
        return "I don't have anything remembered yet."

    query = _fact_words(key)
    scored = []
    for k, v in facts.items():
        # The key is what the fact is ABOUT, so it counts double; the value
        # still helps ("python" finds favorite_programming_language).
        s = 2 * _word_overlap(query, _fact_words(k)) + _word_overlap(query, _fact_words(str(v)))
        if s > 0:
            scored.append((s, k, v))
    scored.sort(key=lambda t: -t[0])

    if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        _, k, v = scored[0]
        return f"{k}: {v}"
    if scored:
        top = "\n".join(f"{k}: {v}" for _, k, v in scored[:3])
        return f"Closest matches for '{key}':\n{top}"
    known = ", ".join(list(facts)[:15])
    return (f"I don't have anything remembered for '{key}'. "
            f"Facts I do have are filed under: {known}.")


def list_known_facts() -> str:
    """List every fact currently remembered about the user."""
    facts = _load_facts()
    if not facts:
        return "I don't have anything remembered yet."
    return "\n".join(f"{k}: {v}" for k, v in facts.items())


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
        return "No skills installed yet."
    names = sorted(f[:-3] for f in os.listdir(SKILLS_DIR) if f.endswith(".md"))
    if not names:
        return "No skills installed yet."
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
        return f"No skill named '{name}'. Use list_skills to see what's available."
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# Registry mapping tool name -> callable, used by brain.py's tool-use loop.
def add_task(text: str, due: str = "") -> str:
    """Add something Chuen has to do, with an optional deadline in his own
    words. Returns confirmation including how the deadline was understood,
    so a misread date is visible immediately rather than at the deadline."""
    t = tasks.add_task(text, due)
    if t["due"]:
        return f"Added: {tasks.describe(t)}"
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
    where = f"The list is kept in {tasks.TASKS_FILE}."
    # Pick up anything ticked in the Obsidian note before answering, so the
    # two views never disagree in front of him.
    ticked = tasks.pull_vault_ticks()
    open_tasks = tasks.list_tasks()
    if ticked:
        names = ", ".join(t["text"] for t in ticked)
        where = f"(Closed {len(ticked)} you ticked in Obsidian: {names}.) " + where
    if not open_tasks:
        return f"Nothing on your list. {where}"
    overdue, soon = tasks.due_within(48)
    over_ids = {t["id"] for t in overdue}
    lines = []
    for t in open_tasks:
        mark = "OVERDUE - " if t["id"] in over_ids else ""
        lines.append(f"- {mark}{tasks.describe(t)}")
    return f"{len(open_tasks)} open:\n" + "\n".join(lines) + f"\n{where}"


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
        return (f"No single open task matched \"{task}\" -- either it isn't "
                "there or more than one matched. Say the exact task name.")
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
        return f"No single open task matched \"{task}\"."
    return f"Removed: {t['text']}"


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
}
