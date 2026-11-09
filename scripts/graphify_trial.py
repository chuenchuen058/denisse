"""Measure how much of a Claude-lane Orchestra run is file re-reading.

Reads one or more Claude Code stream-json lane logs (``.review/reports/<task>.lane.log``)
and splits the run's context into what a code graph could and could not replace.

For every tool result it records the characters returned and how many later API
calls in the same thread carried it again (each later call re-reads the whole
prefix from cache). ``chars / chars_per_token * later_calls`` is that result's
share of the run's cache-read tokens. Summed by category, this gives the upper
bound on what Graphify-style lookups could save: it could at best replace code
exploration (first reads of files the agent did not edit, re-reads, searches),
never reads of files the agent edits (Edit needs a Read first), the task/report
files, or test and git output.

Classification lists (extensions, tool names, shell verbs) live in
``graphify_trial.json`` next to this script.

    .venv\\Scripts\\python.exe scripts\\graphify_trial.py LOG [LOG ...] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "graphify_trial.json"


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _result_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) for b in content if isinstance(b, dict))
    return 0


def _norm(path: str, cwd: str) -> str:
    p = path.replace("\\", "/")
    c = cwd.replace("\\", "/").rstrip("/") + "/"
    if p.lower().startswith(c.lower()):
        p = p[len(c):]
    return p.lstrip("./") if p.startswith("./") else p


_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


def _shell_is_read(cmd: str, cfg: dict) -> bool:
    """True when every segment of a (compound) shell command only reads."""
    if "<<" in cmd:  # heredoc: a script is being run or written
        return False
    bare = _QUOTED.sub("Q", cmd)
    segs = [s.strip() for s in re.split(r"&&|\|\||;|\n|\|", bare) if s.strip()]
    neutral = tuple(cfg["shell_neutral_prefixes"])
    segs = [s for s in segs if not (s.startswith(neutral) or _ASSIGN.match(s))]
    if not segs:
        return False
    verbs = cfg["shell_read_verbs"]
    return all(any(s == v or s.startswith(v + " ") for v in verbs) for s in segs)


def _code_paths(cmd: str, cfg: dict) -> set[str]:
    ext = "|".join(re.escape(e) for e in cfg["code_extensions"])
    return {m.replace("\\", "/") for m in re.findall(rf"[\w*/\\.-]+(?:{ext})\b", cmd)}


def _mentions_code(cmd: str, cfg: dict) -> bool:
    return bool(_code_paths(cmd, cfg))


def analyse(log: Path, cfg: dict) -> dict:
    cwd = ""
    threads_calls: dict[str | None, list[str]] = defaultdict(list)  # thread -> api msg ids in order
    call_usage: dict[str, dict] = {}
    uses: dict[str, dict] = {}
    results: dict[str, int] = {}
    final = {}
    for line in log.open(encoding="utf-8", errors="replace"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            cwd = ev.get("cwd", "")
        elif t == "assistant":
            msg = ev.get("message", {})
            thread = ev.get("parent_tool_use_id")
            mid = msg.get("id")
            if mid and mid not in call_usage:
                threads_calls[thread].append(mid)
                call_usage[mid] = msg.get("usage", {})
            idx = len(threads_calls[thread]) - 1
            for b in msg.get("content", []):
                if b.get("type") == "tool_use":
                    uses[b["id"]] = {"name": b["name"], "input": b.get("input", {}),
                                     "thread": thread, "call": idx}
        elif t == "user":
            for b in (ev.get("message", {}).get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    results[b["tool_use_id"]] = _result_chars(b.get("content"))
        elif t == "result":
            final = ev

    code_ext = tuple(cfg["code_extensions"])
    non_code_dirs = tuple(d.rstrip("/") + "/" for d in cfg["non_code_dirs"])
    edited = set()
    for u in uses.values():
        if u["name"] in cfg["edit_tools"]:
            fp = u["input"].get("file_path") or u["input"].get("notebook_path")
            if fp:
                edited.add(_norm(fp, cwd))

    cpt = float(cfg["chars_per_token"])
    cat_chars: Counter = Counter()
    cat_weighted: Counter = Counter()
    cat_count: Counter = Counter()
    seen_reads: set = set()
    code_files_read: set = set()
    rereads = 0
    for uid, u in sorted(uses.items(), key=lambda kv: (str(kv[1]["thread"]), kv[1]["call"])):
        chars = results.get(uid, 0)
        later = max(len(threads_calls[u["thread"]]) - 1 - u["call"], 0)
        name = u["name"]
        if name in cfg["read_tools"]:
            fp = _norm(u["input"].get("file_path", ""), cwd)
            is_code = fp.endswith(code_ext) and not fp.startswith(non_code_dirs) and ":" not in fp[:3]
            if fp in seen_reads:
                cat = "reread"
                rereads += 1
            elif not is_code:
                cat = "read_noncode"
            elif fp in edited:
                cat = "read_edited"
            else:
                cat = "read_code_explore"
            seen_reads.add(fp)
            if is_code:
                code_files_read.add(fp)
        elif name in cfg["search_tools"]:
            cat = "search"
        elif name in cfg["shell_tools"]:
            cmd = u["input"].get("command", "")
            if not _shell_is_read(cmd, cfg):
                cat = "shell_other"
            elif _mentions_code(cmd, cfg):
                cat = "shell_read_code"
                code_files_read.update(_code_paths(cmd, cfg))
            else:
                cat = "shell_read_noncode"
        elif name in cfg["edit_tools"]:
            cat = "edit"
        else:
            cat = "other"
        cat_chars[cat] += chars
        cat_count[cat] += 1
        cat_weighted[cat] += chars / cpt * later

    main = threads_calls.get(None, [])
    first = call_usage.get(main[0], {}) if main else {}
    prefix = first.get("cache_read_input_tokens", 0) + first.get("cache_creation_input_tokens", 0) + first.get("input_tokens", 0)
    usage = final.get("usage", {})
    summed_cache_read = sum(u.get("cache_read_input_tokens", 0) for u in call_usage.values())
    return {
        "log": log.name,
        "api_calls_main": len(main),
        "api_calls_sub": sum(len(v) for k, v in threads_calls.items() if k is not None),
        "cache_read_total": usage.get("cache_read_input_tokens", summed_cache_read),
        "cache_read_summed_calls": summed_cache_read,
        "output_tokens": usage.get("output_tokens", 0),
        "cost_usd": final.get("total_cost_usd"),
        "start_prefix_tokens": prefix,
        "start_prefix_weighted": prefix * max(len(main) - 1, 0),
        "edited_files": sorted(edited),
        "code_files_read": len(code_files_read),
        "rereads": rereads,
        "by_category": {c: {"n": cat_count[c], "chars": cat_chars[c], "cache_read_est": round(cat_weighted[c])}
                        for c in sorted(cat_count)},
    }


GRAPH_ADDRESSABLE = ("read_code_explore", "reread", "search", "shell_read_code")


def summary_line(r: dict) -> str:
    total = r["cache_read_summed_calls"] or 1
    addr = sum(r["by_category"].get(c, {}).get("cache_read_est", 0) for c in GRAPH_ADDRESSABLE)
    return (f"{r['log']}: calls {r['api_calls_main']}+{r['api_calls_sub']} sub, cache-read {r['cache_read_summed_calls']:,}, "
            f"start prefix {r['start_prefix_tokens']:,} x{r['api_calls_main'] - 1} = {r['start_prefix_weighted']:,} "
            f"({r['start_prefix_weighted'] / total:.0%}), graph-addressable <= {addr:,} ({addr / total:.0%}), "
            f"code files read {r['code_files_read']}, re-reads {r['rereads']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="+", type=Path)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--json", action="store_true", help="print full per-log breakdown as JSON")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    out = [analyse(p, cfg) for p in a.logs]
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        for r in out:
            print(summary_line(r))
            for c, v in r["by_category"].items():
                print(f"    {c:18} n={v['n']:3} chars={v['chars']:8,} cache-read~{v['cache_read_est']:10,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
