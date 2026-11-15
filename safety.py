"""
Confirmation gate for tool calls that touch the real machine. These are
the calls a prompt injection hidden in something Denisse reads (a file,
a web page) could try to trigger without you actually asking for it, so
they get a human check before running.

Which tools are gated is data: tool_policy.json `confirm_first` (moved out
of a literal here on 2026-11-13, AGENTS.md Rule 0), with the reason for
each beside it there.

run_shell_command, write_file, and open_application are all auto-allowed
per user request (run_shell_command/write_file on 2026-09-02,
open_application on 2026-09-03 -- the popup asking to confirm opening
Spotify) -- none of them prompt anymore. To re-gate any of them, add its
name to confirm_first.

That applies to typed requests. Hands-free (wake_word.py) is different:
since 2026-10-18 it asks out loud before run_shell_command, write_file and
anything in RISKY_TOOLS, and takes a spoken yes/no -- a misheard phrase in
a room shouldn't become a shell command. DENISSE_VOICE_CONFIRM=0 turns that
back off (RISKY_TOOLS are still asked about, by voice). A call nobody is
there to answer (tray._decline_risky_unattended) declines them.

delegate is gated since 2026-11-13 (audit 10 Nov, D2): it sends his words
and read access to his folders to Claude or Codex and uses his allowance,
so he sees what goes to which engine and says yes first.
"""

import tool_policy

RISKY_TOOLS = tool_policy.confirm_first()


def confirm_risky_tool(name: str, args: dict) -> bool:
    """CLI confirmation prompt. Returns True if the call should proceed."""
    if name not in RISKY_TOOLS:
        return True
    summary = ", ".join(f"{k}={v!r}" for k, v in args.items())
    answer = input(f"\n[Denisse wants to run {name}({summary})] Allow? [y/N] ").strip().lower()
    return answer in ("y", "yes")
