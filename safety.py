"""
Confirmation gate for tool calls that touch the real machine. These are
the calls a prompt injection hidden in something Denisse reads (a file,
a web page) could try to trigger without you actually asking for it, so
they get a human check before running.

run_shell_command, write_file, and open_application are all auto-allowed
per user request (run_shell_command/write_file on 2026-09-02,
open_application on 2026-09-03 -- the popup asking to confirm opening
Spotify) -- none of them prompt anymore. To re-gate any of them, add its
name back to RISKY_TOOLS below.

disable_startup_program is still gated -- it's reversible
(restore_startup_program undoes it), but changes what launches at login,
which is worth a human glance rather than running silently.
"""

RISKY_TOOLS = {"disable_startup_program"}


def confirm_risky_tool(name: str, args: dict) -> bool:
    """CLI confirmation prompt. Returns True if the call should proceed."""
    if name not in RISKY_TOOLS:
        return True
    summary = ", ".join(f"{k}={v!r}" for k, v in args.items())
    answer = input(f"\n[Denisse wants to run {name}({summary})] Allow? [y/N] ").strip().lower()
    return answer in ("y", "yes")
