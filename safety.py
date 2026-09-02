"""
Confirmation gate for tool calls that touch the real machine (shell
commands, file writes, opening applications). These are the calls a
prompt injection hidden in something Denisse reads (a file, a web page)
could try to trigger without you actually asking for it, so they get a
human check before running.
"""

RISKY_TOOLS = {"run_shell_command", "write_file", "open_application"}


def confirm_risky_tool(name: str, args: dict) -> bool:
    """CLI confirmation prompt. Returns True if the call should proceed."""
    if name not in RISKY_TOOLS:
        return True
    summary = ", ".join(f"{k}={v!r}" for k, v in args.items())
    answer = input(f"\n[Denisse wants to run {name}({summary})] Allow? [y/N] ").strip().lower()
    return answer in ("y", "yes")
