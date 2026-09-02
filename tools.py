"""
Local system/task tools Denisse can call.

Each function here corresponds to a tool definition in brain.py's TOOLS list.
Keep these narrow and predictable — the LLM decides *when* to call them,
but the actual side effects should stay easy to reason about and safe
to run unattended.
"""

import datetime
import json
import os
import subprocess
import platform

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
        timeout=15,
    )
    app_id = result.stdout.strip()
    if not app_id:
        return False
    subprocess.run(["explorer.exe", f"shell:AppsFolder\\{app_id}"], check=False)
    return True


def open_application(app_name: str) -> str:
    """Open a local application by name (best-effort, OS-dependent)."""
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

    return f"Couldn't find an app matching '{app_name}' (tried a direct launch and a Start Menu search)."


def list_directory(path: str = ".") -> str:
    """List files and folders at a given path."""
    try:
        entries = os.listdir(path)
        return "\n".join(entries) if entries else "(empty directory)"
    except Exception as e:
        return f"Error: {e}"


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


def get_current_time() -> str:
    """Return the current local date and time."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def remember(key: str, value: str) -> str:
    """Save a fact about the user for later recall (e.g. key='favorite_coffee', value='flat white')."""
    facts = _load_facts()
    facts[key] = value
    _save_facts(facts)
    return f"Got it, I'll remember that {key} = {value}."


def recall(key: str) -> str:
    """Recall a previously remembered fact by its key."""
    facts = _load_facts()
    return facts.get(key, f"I don't have anything remembered for '{key}'.")


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
TOOL_FUNCTIONS = {
    "run_shell_command": run_shell_command,
    "open_application": open_application,
    "list_directory": list_directory,
    "read_file": read_file,
    "write_file": write_file,
    "get_current_time": get_current_time,
    "remember": remember,
    "recall": recall,
    "list_known_facts": list_known_facts,
    "list_skills": list_skills,
    "read_skill": read_skill,
}
