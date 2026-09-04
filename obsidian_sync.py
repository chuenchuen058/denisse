"""
Mirrors Denisse's own memory (conversation history + remembered facts) into
the Denisse section of Chuen's Obsidian vault, and reads back whatever
Claude has written there so Denisse could fold it into context.

The vault is Claude's memory bank; Denisse writes into one section of it
(`Denisse/Brain`, `Denisse/News`). The read-back half is currently disabled
in brain.py -- see read_claude_memory() below.

Best-effort only: if the vault (e.g. an unsynced OneDrive path) isn't
there, every function here is a silent no-op -- this must never be why a
normal chat turn fails.
"""

import datetime
import json
import os
import re

# The vault is Claude's memory bank, not Denisse's -- Denisse is one section
# inside it (restructured 2026-09-04). The folder is still named "Denisse"
# because it's the registered Obsidian vault path; only the layout moved.
VAULT_DIR = r"C:\Users\Xichu\OneDrive - King's College London\Denisse"
DENISSE_DIR = os.path.join(VAULT_DIR, "Denisse")
BRAIN_DIR = os.path.join(DENISSE_DIR, "Brain")
CLAUDE_MEMORY_DIR = os.path.join(VAULT_DIR, "Memory")
NEWS_DIR = os.path.join(DENISSE_DIR, "News")

# The full, unfiltered contents of Memory/ (4KB+ of notes, including YAML
# frontmatter and meta-commentary about another AI session's own past
# confusion about Denisse's identity) measurably broke her replies when
# injected whole -- tested directly: the exact same question came back
# wrong 3/3 times with it in the system prompt (even at low sampling
# temperature) and correct 4/4 times with it removed. Trimmed rather than
# dropped entirely, since the cross-tool memory sharing is a deliberate
# feature (see claude_vault.md in the vault) -- just capped hard and with
# the noisiest part (frontmatter) stripped, so a little context still
# comes through without dominating/confusing her.
_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
MAX_CLAUDE_CONTEXT_CHARS = 600


def _vault_available() -> bool:
    return os.path.isdir(VAULT_DIR)


def sync_conversation(messages: list) -> None:
    """Mirror the conversation history into the vault as JSON (for
    round-tripping) and Markdown (for reading in Obsidian)."""
    if not _vault_available():
        return
    try:
        os.makedirs(BRAIN_DIR, exist_ok=True)
        with open(os.path.join(BRAIN_DIR, "conversation.json"), "w", encoding="utf-8") as f:
            json.dump(messages, f, indent=2)

        lines = ["# Denisse Conversation", ""]
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content", "")
            if not content:
                continue
            lines.append(f"**{role}:** {content}")
            lines.append("")
        with open(os.path.join(BRAIN_DIR, "conversation.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except OSError:
        pass


def sync_facts(facts: dict) -> None:
    """Mirror remembered facts into the vault as JSON and Markdown."""
    if not _vault_available():
        return
    try:
        os.makedirs(BRAIN_DIR, exist_ok=True)
        with open(os.path.join(BRAIN_DIR, "facts.json"), "w", encoding="utf-8") as f:
            json.dump(facts, f, indent=2)

        lines = ["# Denisse: Known Facts", ""]
        for k, v in facts.items():
            lines.append(f"- **{k}:** {v}")
        with open(os.path.join(BRAIN_DIR, "facts.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except OSError:
        pass


def read_claude_memory() -> str:
    """Read whatever Claude Code / Claude Desktop have written to the
    shared vault's Memory folder, concatenated for use as extra context --
    trimmed hard (frontmatter stripped, length capped at
    MAX_CLAUDE_CONTEXT_CHARS) since the raw unfiltered version measurably
    degraded reply quality (see the module comment above)."""
    if not _vault_available() or not os.path.isdir(CLAUDE_MEMORY_DIR):
        return ""
    parts = []
    try:
        for name in sorted(os.listdir(CLAUDE_MEMORY_DIR)):
            if not name.endswith(".md"):
                continue
            try:
                with open(os.path.join(CLAUDE_MEMORY_DIR, name), "r", encoding="utf-8") as f:
                    content = _FRONTMATTER_RE.sub("", f.read()).strip()
            except OSError:
                continue
            if content:
                parts.append(content)
    except OSError:
        return ""
    combined = "\n\n---\n\n".join(parts)
    if len(combined) > MAX_CLAUDE_CONTEXT_CHARS:
        combined = combined[:MAX_CLAUDE_CONTEXT_CHARS].rstrip() + "…"
    return combined


def save_daily_news(summary: str, headlines: list) -> bool:
    """Save the day's CNA news summary (see tray.py's daily briefing loop
    and news.py) as a dated note under News/ in the vault -- one file per
    calendar day, overwritten if called again the same day. Returns True
    on an actual write, False on anything that stopped it (no vault, no
    summary) so the caller can decide whether to log/notify -- best-effort
    like everything else here, never raises."""
    if not _vault_available() or not summary:
        return False
    try:
        os.makedirs(NEWS_DIR, exist_ok=True)
        today = datetime.date.today().isoformat()
        lines = [
            "---",
            "tags: [denisse, news]",
            f"date: {today}",
            "---",
            "",
            f"# CNA News — {today}",
            "",
            summary.strip(),
        ]
        if headlines:
            lines += ["", "## Headlines", ""]
            lines += [f"- {h}" for h in headlines]
        with open(os.path.join(NEWS_DIR, f"{today}.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return True
    except OSError:
        return False
