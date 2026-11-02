"""Watch what Orchestra's agents are doing right now, live.

    .venv\\Scripts\\python.exe orchestra\\watch.py          # follow every running task
    .venv\\Scripts\\python.exe orchestra\\watch.py --once   # print the current state and exit

Reads the slot lock files (.review/runner.*.lock) to see which tasks are running and follows
their lane logs. Claude lanes write stream-json (one event per line); Codex lanes write plain
text. Read-only: it never touches the queue, state or worktrees.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(os.environ.get("ORCHESTRA_REPO", Path(__file__).resolve().parent.parent))
REVIEW = REPO / ".review"
COLOURS = ["\033[96m", "\033[95m", "\033[93m", "\033[92m", "\033[94m"]
RESET, DIM = "\033[0m", "\033[2m"


def running() -> dict[str, str]:
    """slot name -> task slug"""
    out = {}
    for lf in sorted(REVIEW.glob("runner.*.lock")):
        try:
            slug = json.loads(lf.read_text(encoding="utf-8")).get("task")
        except (OSError, ValueError):
            continue
        if slug:
            out[lf.stem.replace("runner.", "")] = slug
    return out


def short(s: str, n: int = 160) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe(line: str) -> str | None:
    """One readable line for a stream-json event, or the raw text for plain logs."""
    line = line.strip()
    if not line:
        return None
    if not line.startswith("{"):
        if "CategoryInfo" in line or "FullyQualifiedErrorId" in line:
            return None  # PowerShell profile noise
        return short(line)
    try:
        ev = json.loads(line)
    except ValueError:
        return short(line)
    t = ev.get("type")
    if t == "assistant":
        parts = []
        for c in (ev.get("message") or {}).get("content") or []:
            if c.get("type") == "text" and c.get("text", "").strip():
                parts.append("💬 " + short(c["text"]))
            elif c.get("type") == "tool_use":
                inp = c.get("input") or {}
                arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("path") or ""
                parts.append(f"🔧 {c.get('name')} {short(arg, 120)}")
        return "\n".join(parts) or None
    if t == "result":
        cost = ev.get("total_cost_usd")
        return f"✅ finished ({ev.get('subtype')}), {ev.get('num_turns')} turns" + (f", ${cost:.2f}" if cost else "")
    if t == "system" and ev.get("subtype") == "init":
        return f"▶ session started ({ev.get('model', '?')})"
    return None


@dataclass
class WatchState:
    tail: int = 8
    offsets: dict[str, int] = field(default_factory=dict)
    colour: dict[str, str] = field(default_factory=dict)
    last_banner: str | None = None


def poll_once(state: WatchState) -> None:
    """Print new lane events, remembering offsets and display state for the next poll."""
    now = running()
    banner = ", ".join(f"{s}: {t}" for s, t in now.items()) or "nothing running"
    if banner != state.last_banner:
        print(f"\n{DIM}{time.strftime('%H:%M:%S')}  running → {banner}{RESET}")
        state.last_banner = banner
    for slot, slug in now.items():
        log = REVIEW / "reports" / f"{slug}.lane.log"
        c = state.colour.setdefault(slug, COLOURS[len(state.colour) % len(COLOURS)])
        if not log.exists():
            continue
        with log.open("r", encoding="utf-8", errors="replace") as f:
            if slug not in state.offsets:  # first sight: show only the last few events
                lines = f.readlines()
                shown = [d for d in map(describe, lines) if d][-state.tail:]
                for d in shown:
                    for l in d.splitlines():
                        print(f"{c}[{slug[17:45]}]{RESET} {l}")
                state.offsets[slug] = f.tell()
                continue
            offset = state.offsets[slug]
            if os.fstat(f.fileno()).st_size < offset:  # log replaced or truncated between polls
                offset = 0
            f.seek(offset)
            for line in f:
                d = describe(line)
                if d:
                    for l in d.splitlines():
                        print(f"{c}[{slug[17:45]}]{RESET} {l}")
            state.offsets[slug] = f.tell()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="print current state and exit")
    ap.add_argument("--tail", type=int, default=8, help="events to show per task on start")
    args = ap.parse_args()
    if os.name == "nt":
        os.system("")  # enable ANSI colours in the Windows console
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    state = WatchState(tail=args.tail)
    while True:
        poll_once(state)
        if args.once:
            return 0
        sys.stdout.flush()
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
