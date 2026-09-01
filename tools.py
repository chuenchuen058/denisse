"""
Local system/task tools Denisse can call.

Each function here corresponds to a tool definition in brain.py's TOOLS list.
Keep these narrow and predictable — the LLM decides *when* to call them,
but the actual side effects should stay easy to reason about and safe
to run unattended.
"""

import datetime
import os
import subprocess
import platform


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


def open_application(app_name: str) -> str:
    """Open a local application by name (best-effort, OS-dependent)."""
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(app_name)  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.run(["open", "-a", app_name], check=True)
        else:
            subprocess.run([app_name], check=True)
        return f"Opened {app_name}"
    except Exception as e:
        return f"Error opening {app_name}: {e}"


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


# Registry mapping tool name -> callable, used by brain.py's tool-use loop.
TOOL_FUNCTIONS = {
    "run_shell_command": run_shell_command,
    "open_application": open_application,
    "list_directory": list_directory,
    "read_file": read_file,
    "write_file": write_file,
    "get_current_time": get_current_time,
}
