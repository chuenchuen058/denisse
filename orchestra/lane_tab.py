"""One lane, live, in its own Windows Terminal tab (Chuen, 9 Nov 2026: "I want it to be
autonomous ... so I don't have to put it in myself"; he wants to watch Claude Code work).

The runner still runs the lane headless (`claude -p --output-format stream-json`), so the
report file, the timeout and kill-on-close job, the cost capture, the worktree, the guard
hook and the Don consult are exactly as before. This only *reads* the lane log the runner
already writes and renders it the way Claude Code shows a session: assistant text, every
tool call with its file or command, Edit/Write diffs, tool errors, the final result. Codex
and Copilot logs are plain text and are shown as they come.

    python orchestra\\lane_tab.py --log <slug>.lane.log --done <slug>.lane.done --title "Claude: ..."

It ends itself: when the runner writes the done file (the lane exited) it prints the DONE
line, waits lane_tab_linger_s and exits, which closes the tab. A runner that died never
writes it, so --max-s (the lane timeout plus lane_tab_extra_s) ends it anyway. Read-only:
it never touches the queue, state, the worktree or the lane.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
RED, GREEN, YELLOW, CYAN, MAGENTA = "\033[91m", "\033[92m", "\033[93m", "\033[96m", "\033[95m"


def load_cfg() -> dict:
    """config_defaults.json overlaid with config.json (notes dropped). The runner validates the
    same files; here a bad value only changes how the tab looks, so no validation."""
    out: dict = {}
    for name in ("config_defaults.json", "config.json"):
        try:
            data = json.loads((HERE / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.update({k: v for k, v in data.items() if not k.startswith("_")})
    return out


def _clip(text: str, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


def _lines(text: str, max_lines: int) -> list[str]:
    lines = str(text).splitlines() or [""]
    if len(lines) > max_lines:
        return lines[:max_lines] + [f"… ({len(lines) - max_lines} more lines)"]
    return lines


def tool_call(name: str, inp: dict, cfg: dict) -> list[str]:
    """A tool call the way Claude Code shows it: the tool and its target, then any diff."""
    width, diff_n = int(cfg.get("lane_tab_width", 160)), int(cfg.get("lane_tab_diff_lines", 40))
    target = (inp.get("command") or inp.get("file_path") or inp.get("notebook_path") or inp.get("pattern")
              or inp.get("path") or inp.get("url") or inp.get("description") or inp.get("prompt") or "")
    head = f"{CYAN}● {name}{RESET}"
    if target:
        head += f"({_clip(' '.join(str(target).split()), width)})"
    out = [head]
    if name == "Bash" and inp.get("command") and "\n" in str(inp["command"]):
        out += [f"  {DIM}${RESET} {l}" for l in _lines(inp["command"], diff_n)]
    edits = inp.get("edits") if isinstance(inp.get("edits"), list) else [inp]
    for e in edits:
        if not isinstance(e, dict):
            continue
        if "old_string" in e or "new_string" in e:
            out += [f"  {RED}- {l}{RESET}" for l in _lines(e.get("old_string", ""), diff_n)]
            out += [f"  {GREEN}+ {l}{RESET}" for l in _lines(e.get("new_string", ""), diff_n)]
    if name == "Write" and isinstance(inp.get("content"), str):
        body = inp["content"]
        out += [f"  {GREEN}+ {l}{RESET}" for l in _lines(body, diff_n)]
    return out


def tool_result(content, is_error: bool, cfg: dict) -> list[str]:
    """One dim line per result (errors in red, a few lines): enough to follow, not a dump."""
    if isinstance(content, list):
        content = "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    text = str(content or "").strip()
    if not text:
        return []
    width = int(cfg.get("lane_tab_width", 160))
    if is_error:
        return [f"  {RED}⎿ {_clip(l, width)}{RESET}" for l in _lines(text, int(cfg.get("lane_tab_error_lines", 6)))]
    first = text.splitlines()[0]
    more = len(text.splitlines()) - 1
    return [f"  {DIM}⎿ {_clip(first, width)}{f' (+{more} lines)' if more else ''}{RESET}"]


def render(line: str, cfg: dict) -> list[str]:
    """Display lines for one lane-log line (stream-json event, or plain text)."""
    line = line.rstrip("\r\n")
    if not line.strip():
        return []
    if not line.lstrip().startswith("{"):
        return [line]
    try:
        ev = json.loads(line)
    except ValueError:
        return [line]
    if not isinstance(ev, dict):
        return [line]
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        return [f"{MAGENTA}▶ session started{RESET} {DIM}model {ev.get('model', '?')}, cwd {ev.get('cwd', '?')}{RESET}"]
    if t == "assistant":
        out = []
        for c in (ev.get("message") or {}).get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and str(c.get("text", "")).strip():
                out += ["", f"{BOLD}{c['text'].strip()}{RESET}"]
            elif c.get("type") == "tool_use":
                out += tool_call(str(c.get("name", "?")), c.get("input") or {}, cfg)
        return out
    if t == "user":
        out = []
        content = (ev.get("message") or {}).get("content")
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                out += tool_result(c.get("content"), bool(c.get("is_error")), cfg)
        return out
    if t == "result":
        cost = ev.get("total_cost_usd")
        secs = (ev.get("duration_ms") or 0) / 1000
        bits = [str(ev.get("subtype", "?")), f"{ev.get('num_turns', '?')} turns", f"{secs / 60:.1f} min"]
        if isinstance(cost, (int, float)):
            bits.append(f"${cost:.2f}")
        colour = RED if ev.get("is_error") else GREEN
        return ["", f"{colour}■ session finished: {', '.join(bits)}{RESET}"]
    if t == "rate_limit_event":
        info = ev.get("rate_limit_info") or {}
        if info.get("status") == "rejected":
            return [f"{YELLOW}⚠ usage limit reached ({info.get('rateLimitType', '?')}){RESET}"]
    return []


def done_text(done: Path, cfg: dict, linger: int) -> str:
    try:
        data = json.loads(done.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    try:
        return str(cfg.get("lane_tab_done_text", "DONE: {result}")).format(
            result=data.get("result") or data.get("outcome") or "?", outcome=data.get("outcome", "?"),
            linger=linger)
    except (KeyError, IndexError, ValueError):
        return f"DONE: {data.get('result') or data.get('outcome') or '?'}"


def follow(log: Path, done: Path, cfg: dict, *, max_s: float, linger: float, out=print,
           sleep=time.sleep, clock=time.monotonic) -> str:
    """Render the log as it grows until the done file appears (or max_s passes). Returns
    'done' or 'max'. `out`, `sleep`, `clock` are injected by the tests."""
    poll = float(cfg.get("lane_tab_poll_s", 0.5))
    start, offset, partial = clock(), 0, ""

    def drain() -> None:
        nonlocal offset, partial
        try:
            with log.open("r", encoding="utf-8", errors="replace") as f:
                if os.fstat(f.fileno()).st_size < offset:  # log replaced (a retry): start again
                    offset, partial = 0, ""
                f.seek(offset)
                chunk = f.read()
                offset = f.tell()
        except OSError:
            return
        text = partial + chunk
        lines = text.split("\n")
        partial = lines.pop()  # an unfinished line waits for the rest
        for l in lines:
            for shown in render(l, cfg):
                out(shown)

    while True:
        drain()
        if done.exists():
            drain()
            if partial:
                for shown in render(partial, cfg):
                    out(shown)
            out("")
            out(f"{GREEN}{BOLD}{done_text(done, cfg, int(linger))}{RESET}")
            sleep(linger)
            return "done"
        if clock() - start > max_s:
            out("")
            out(f"{YELLOW}{cfg.get('lane_tab_max_text', 'Stopped following: no DONE from the runner in time.')}{RESET}")
            sleep(linger)
            return "max"
        sleep(poll)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", required=True)
    ap.add_argument("--done", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--max-s", type=float, required=True)
    ap.add_argument("--linger-s", type=float, required=True)
    a = ap.parse_args(argv)
    if os.name == "nt":
        os.system("")  # ANSI colours in a plain console
        if a.title:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(a.title)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    print(f"{BOLD}{a.title}{RESET}", flush=True)
    follow(Path(a.log), Path(a.done), load_cfg(), max_s=a.max_s, linger=a.linger_s,
           out=lambda s: print(s, flush=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
