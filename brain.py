"""
The local brain: talks to a model running on this machine via Ollama
(http://localhost:11434), defines the tool schema, and runs the tool-use
loop that turns a user message into a final text reply (after any tool
calls).

No API key, no internet required after the model is downloaded once.
"""

import os

import ollama
from dotenv import load_dotenv

import tools

load_dotenv()

MODEL = os.environ.get("DENISSE_MODEL", "llama3.1:8b")

SYSTEM_PROMPT = (
    "You are Denisse, a personal AI assistant running locally on the user's "
    "own computer. You can hold a conversation and, when it genuinely helps, "
    "use your tools to check the time, run shell commands, open "
    "applications, read/write files, and remember/recall facts about the "
    "user. Be direct and concise. Only use a tool when it's actually needed "
    "to answer or act -- don't narrate your intentions, just do it and "
    "report the result. When you learn something worth remembering about "
    "the user (their name, preferences, ongoing projects), use the "
    "'remember' tool to save it without being asked. You also have a "
    "skills library -- specific how-to guides for particular kinds of "
    "tasks. Call 'list_skills' to see what's available, and 'read_skill' "
    "to load one BEFORE attempting a task it covers (e.g. organizing "
    "files, giving a daily briefing, taking notes) -- follow its "
    "instructions rather than improvising."
)

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell_command",
            "description": "Run a shell command on the local machine and return its output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The shell command to run."}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": "Open a local application by name.",
            "parameters": {
                "type": "object",
                "properties": {"app_name": {"type": "string", "description": "Name or path of the application to open."}},
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and folders at a given path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path to list. Defaults to current directory."}},
                "required": [],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the text contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path to the file to read."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a file, overwriting it if it already exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to write."},
                    "content": {"type": "string", "description": "Text content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current local date and time.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },

    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a fact about the user for later recall, e.g. remember('favorite_coffee', 'flat white').",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short identifier for the fact, e.g. 'favorite_coffee'."},
                    "value": {"type": "string", "description": "The fact to store."},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Recall a previously remembered fact about the user by its key.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "The identifier used when the fact was saved."}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_known_facts",
            "description": "List every fact currently remembered about the user.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "List available skills (how-to guides for specific kinds of tasks).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": "Read the full instructions for one skill by name (see list_skills).",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Skill name, e.g. 'organize_files'."}},
                "required": ["name"],
            },
        },
    },
]

def _get(obj, key, default=None):
    """Works whether ollama hands back a dict or an attribute-style object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class Brain:
    def __init__(self):
        try:
            local_models = [m.get("model", m.get("name", "")) for m in ollama.list().get("models", [])]
        except Exception as e:
            raise RuntimeError(
                f"Can't reach the local Ollama server: {e}. "
                "Make sure Ollama is installed and running (it should start "
                "automatically -- check for its icon in the system tray)."
            )
        if not any(MODEL in m for m in local_models):
            raise RuntimeError(
                f"Model '{MODEL}' isn't downloaded yet. Run: ollama pull {MODEL}"
            )

    def respond(self, messages: list) -> tuple[str, list]:
        """
        Run one turn of the tool-use loop against `messages` (role/content
        dicts, not including the system prompt). Returns
        (final_text, updated_messages).
        """
        full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

        while True:
            response = ollama.chat(model=MODEL, messages=full_messages, tools=TOOL_DEFINITIONS)
            msg = _get(response, "message")
            tool_calls = _get(msg, "tool_calls") or []

            if not tool_calls:
                final_text = _get(msg, "content", "") or ""
                messages.append({"role": "assistant", "content": final_text})
                return final_text, messages

            # Model wants to use one or more tools.
            assistant_msg = {"role": "assistant", "content": _get(msg, "content", "") or ""}
            messages.append(assistant_msg)
            full_messages.append(assistant_msg)

            for call in tool_calls:
                func_info = _get(call, "function")
                name = _get(func_info, "name")
                args = dict(_get(func_info, "arguments") or {})

                func = tools.TOOL_FUNCTIONS.get(name)
                if func is None:
                    result = f"Error: unknown tool '{name}'"
                else:
                    try:
                        result = func(**args)
                    except Exception as e:
                        result = f"Error running '{name}': {e}"

                tool_msg = {"role": "tool", "content": str(result)}
                messages.append(tool_msg)
                full_messages.append(tool_msg)
            # loop again so the model can use the tool results
