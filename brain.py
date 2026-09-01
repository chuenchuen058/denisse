"""
The Claude-backed brain: wraps the Anthropic API, holds the tool-use loop,
and turns a user message into a final text reply (after any tool calls).
"""

import os
import anthropic
from dotenv import load_dotenv

import tools

load_dotenv()

MODEL = os.environ.get("DENISSE_MODEL", "claude-sonnet-4-5")

SYSTEM_PROMPT = (
    "You are Denisse, a personal AI assistant running locally on the user's "
    "computer. You can hold a conversation and, when it genuinely helps, use "
    "your tools to check the time, run shell commands, open applications, "
    "and read/write/list files on this machine. Be direct and concise. "
    "Only use a tool when it's actually needed to answer or act — don't "
    "narrate your intentions, just do it and report the result."
)

TOOL_DEFINITIONS = [
    {
        "name": "run_shell_command",
        "description": "Run a shell command on the local machine and return its output.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The shell command to run."}},
            "required": ["command"],
        },
    },
    {
        "name": "open_application",
        "description": "Open a local application by name.",
        "input_schema": {
            "type": "object",
            "properties": {"app_name": {"type": "string", "description": "Name or path of the application to open."}},
            "required": ["app_name"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and folders at a given path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory path to list. Defaults to current directory."}},
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": "Read the text contents of a file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path to the file to read."}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write text content to a file, overwriting it if it already exists.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to write."},
                "content": {"type": "string", "description": "Text content to write."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "get_current_time",
        "description": "Get the current local date and time.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


class Brain:
    def __init__(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key or api_key == "your-api-key-here":
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        self.client = anthropic.Anthropic(api_key=api_key)

    def respond(self, messages: list) -> tuple[str, list]:
        """
        Run one turn of the tool-use loop against `messages` (Anthropic message
        format, not including the system prompt). Returns (final_text, updated_messages).
        """
        while True:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )

            if response.stop_reason != "tool_use":
                final_text = "".join(
                    block.text for block in response.content if block.type == "text"
                )
                messages.append({"role": "assistant", "content": response.content})
                return final_text, messages

            # Model wants to use one or more tools.
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                func = tools.TOOL_FUNCTIONS.get(block.name)
                if func is None:
                    result = f"Error: unknown tool '{block.name}'"
                else:
                    result = func(**block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result),
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            # loop again so the model can use the tool results
