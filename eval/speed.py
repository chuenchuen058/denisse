"""
What makes an eval round slow, and how to run less of it.

Three pieces, each usable on its own and all without a model:

  Profile        wraps the two things a round waits on -- ollama.chat and
                 brain._run_tool -- and keeps Ollama's own per-call numbers
                 (load, prompt prefill, generation, token counts) so a report
                 can say where the seconds went instead of guessing.
  gpu_snapshot   nvidia-smi utilisation and memory plus the models Ollama has
                 loaded, sampled once before the first case. A round that
                 starts with the GPU already busy is flagged noisy, because
                 the 2 Nov voice-latency run measured 17-121 s to first token
                 for exactly that reason and its timings were worthless.
  select_cases   --changed-only: the cases whose code paths a change since a
                 git ref can reach, plus a fixed smoke set. Conservative by
                 design -- a change to anything on every turn's path (brain,
                 the router, the cases themselves) is the full suite.

Measured 2026-11-03 before any of this existed: the model was already warm
(keep_alive=-1, load 0.01 s) and Ollama's prompt cache already hit on the
shared system prefix (1650-token prefill: 0.74 s cold, 0.02 s warm). So the
harness had nothing left to warm up; what this adds is knowing that, and
running fewer cases when fewer are relevant.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ----------------------------------------------------------------- profile


def _field(resp, key):
    if isinstance(resp, dict):
        return resp.get(key)
    return getattr(resp, key, None)


class Profile:
    """Running totals for one process. install() patches brain's view of
    ollama.chat and _run_tool; nothing about the calls themselves changes --
    same arguments, same return value -- only the clock runs alongside."""

    FIELDS = ("calls", "model_s", "load_s", "prefill_s", "gen_s", "prompt_tokens",
              "gen_tokens", "tool_calls", "tool_s")

    def __init__(self):
        for f in self.FIELDS:
            setattr(self, f, 0)
        self.max_gen_tokens = 0
        self.tools = collections.Counter()      # tool name -> seconds
        self._installed = False

    def record_chat(self, resp, wall: float) -> None:
        self.calls += 1
        self.model_s += wall
        ns = 1e-9
        self.load_s += (_field(resp, "load_duration") or 0) * ns
        self.prefill_s += (_field(resp, "prompt_eval_duration") or 0) * ns
        self.gen_s += (_field(resp, "eval_duration") or 0) * ns
        self.prompt_tokens += _field(resp, "prompt_eval_count") or 0
        n = _field(resp, "eval_count") or 0
        self.gen_tokens += n
        self.max_gen_tokens = max(self.max_gen_tokens, n)

    def record_tool(self, name: str, wall: float) -> None:
        self.tool_calls += 1
        self.tool_s += wall
        self.tools[name] += wall

    def snapshot(self) -> dict:
        return {f: getattr(self, f) for f in self.FIELDS}

    @staticmethod
    def delta(before: dict, after: dict) -> dict:
        return {k: round(after[k] - before[k], 3) if isinstance(after[k], float)
                else after[k] - before[k] for k in before}

    def install(self) -> None:
        if self._installed:
            return
        import brain
        prof = self
        real_chat = brain.ollama.chat
        real_tool = brain._run_tool

        def chat(*a, **kw):
            t = time.perf_counter()
            resp = real_chat(*a, **kw)
            prof.record_chat(resp, time.perf_counter() - t)
            return resp

        def run_tool(name, args, confirm, used=None):
            t = time.perf_counter()
            try:
                return real_tool(name, args, confirm, used)
            finally:
                prof.record_tool(name, time.perf_counter() - t)

        brain.ollama.chat = chat
        brain._run_tool = run_tool
        self._installed = True

    def summary(self, wall_s: float, cases_s: float, startup_s: float,
                learning_s: float = 0.0) -> dict:
        """Segments of one round. `other` is the Python between and around
        the calls: building messages, the brain's own matchers and guards,
        the checks. It is what's left, so it absorbs any clock skew too."""
        other = max(0.0, cases_s - self.model_s - self.tool_s)

        def sec(x):
            return round(float(x), 1)
        return {
            "wall_s": sec(wall_s), "startup_s": sec(startup_s),
            "cases_s": sec(cases_s), "model_s": sec(self.model_s),
            "load_s": sec(self.load_s), "prefill_s": sec(self.prefill_s),
            "gen_s": sec(self.gen_s), "tool_s": sec(self.tool_s),
            "other_s": sec(other), "learning_s": sec(learning_s),
            "calls": self.calls, "prompt_tokens": self.prompt_tokens,
            "gen_tokens": self.gen_tokens, "max_gen_tokens": self.max_gen_tokens,
            "tool_calls": self.tool_calls,
            "slowest_tools": [(n, round(s, 1)) for n, s in self.tools.most_common(5)],
        }


def timing_lines(s: dict) -> list:
    """The report section for a Profile.summary()."""
    lines = ["## Where the time went", "",
             "| segment | seconds |", "|---|---|",
             f"| whole run (wall) | {s['wall_s']} |",
             f"| startup: imports, brain, scratch state | {s['startup_s']} |",
             f"| cases, of which: | {s['cases_s']} |",
             f"| &nbsp;&nbsp; model, total wall | {s['model_s']} |",
             f"| &nbsp;&nbsp;&nbsp;&nbsp; load | {s['load_s']} |",
             f"| &nbsp;&nbsp;&nbsp;&nbsp; prompt prefill | {s['prefill_s']} |",
             f"| &nbsp;&nbsp;&nbsp;&nbsp; generation | {s['gen_s']} |",
             f"| &nbsp;&nbsp; tools | {s['tool_s']} |",
             f"| &nbsp;&nbsp; python between calls | {s['other_s']} |"]
    if s.get("learning_s"):
        lines.append(f"| learning pass | {s['learning_s']} |")
    lines += ["",
              f"{s['calls']} model calls, {s['prompt_tokens']} prompt tokens in, "
              f"{s['gen_tokens']} generated (longest reply {s['max_gen_tokens']} tokens); "
              f"{s['tool_calls']} tool calls."]
    if s.get("slowest_tools"):
        lines.append("Slowest tools: " + ", ".join(f"`{n}` {sec}s" for n, sec in s["slowest_tools"]) + ".")
    lines.append("")
    return lines


# ------------------------------------------------------------ gpu snapshot

# A round that begins with the GPU this busy is measuring someone else's
# job. Same knob the local team uses (orchestra/config.json
# local_team_gpu_busy_pct is 60 for "skip the call"); the eval is stricter
# because it reports timings, not just pass/fail.
NOISY_PCT = int(os.environ.get("DENISSE_EVAL_GPU_BUSY_PCT", "30"))


def parse_gpu_csv(text: str) -> dict | None:
    """`nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total
    --format=csv,noheader,nounits` -> {"util_pct", "used_mib", "total_mib"}.
    Several GPUs: the busiest line."""
    best = None
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            row = {"util_pct": int(parts[0]), "used_mib": int(parts[1]), "total_mib": int(parts[2])}
        except ValueError:
            continue
        if best is None or row["util_pct"] > best["util_pct"]:
            best = row
    return best


def gpu_snapshot(samples: int = 2, gap_s: float = 0.5) -> dict:
    """GPU state right now: {"util_pct", "used_mib", "total_mib", "loaded",
    "noisy", "note"}. Missing pieces are None, never an exception -- a laptop
    without nvidia-smi still gets its eval. Two samples half a second apart,
    the busier one kept, so an idle blip isn't read as quiet."""
    snap = {"util_pct": None, "used_mib": None, "total_mib": None, "loaded": [], "noisy": False, "note": ""}
    exe = shutil.which("nvidia-smi")
    if exe:
        best = None
        for i in range(max(1, samples)):
            try:
                out = subprocess.run([exe, "--query-gpu=utilization.gpu,memory.used,memory.total",
                                      "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                     timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                row = parse_gpu_csv(out)
            except (OSError, subprocess.SubprocessError):
                row = None
            if row and (best is None or row["util_pct"] > best["util_pct"]):
                best = row
            if i + 1 < samples:
                time.sleep(gap_s)
        if best:
            snap.update(best)
        else:
            snap["note"] = "nvidia-smi gave nothing readable"
    else:
        snap["note"] = "nvidia-smi not found"
    try:
        import ollama
        snap["loaded"] = sorted(str(_field(m, "model") or _field(m, "name") or "")
                                for m in (_field(ollama.ps(), "models") or []))
    except Exception as exc:  # Ollama down is the eval's problem to report, not ours
        snap["note"] = (snap["note"] + "; " if snap["note"] else "") + f"ollama ps failed: {type(exc).__name__}"
    snap["noisy"] = is_noisy(snap)
    return snap


def is_noisy(snap: dict, limit: int = NOISY_PCT) -> bool:
    util = snap.get("util_pct")
    return util is not None and util >= limit


def gpu_lines(snap: dict, lease: dict | None) -> list:
    """Header lines for the report."""
    if snap.get("util_pct") is None:
        gpu = f"GPU at start: unknown ({snap.get('note') or 'no reading'})"
    else:
        gpu = (f"GPU at start: {snap['util_pct']}% busy, "
               f"{snap['used_mib']}/{snap['total_mib']} MiB used")
    if snap.get("loaded"):
        gpu += ", loaded: " + ", ".join(f"`{m}`" for m in snap["loaded"])
    lines = [f"- {gpu}"]
    if lease:
        if lease.get("held"):
            lines.append("- GPU lease: held for the run")
        else:
            lines.append(f"- GPU lease: **not held** (forced past holder `{lease.get('holder') or '?'}`)")
    if snap.get("noisy"):
        lines += ["", f"> **Noisy run.** The GPU was {snap['util_pct']}% busy before the first case "
                  f"(limit {NOISY_PCT}%). Something else was using it, so the timings here are "
                  "unreliable and a slow reply is not evidence against Denisse."]
    return lines


# ------------------------------------------------------------ changed-only

# Changing any of these changes every turn: the full suite is the only
# honest check.
CORE_FILES = {
    "brain.py", "brain_rules.py", "tool_router.py", "conversation.py", "memory_store.py",
    "safety.py", "instincts.py", "clarify.py", "tools.py",
    "eval/cases.py", "eval/run_eval.py", "eval/speed.py",
}

# Cases that always run under --changed-only: one per code path the brain
# can take, so a change nobody mapped still gets a look. Kept small on
# purpose -- a quick check that takes a full round's time is not quick.
SMOKE = [
    "greet_hey",            # answered in code, no model
    "small_talk",           # model, must call nothing
    "time",                 # model, one core tool
    "math",                 # core tool with an argument to get right
    "find_file",            # files group, real search
    "task_add",             # a write to the scratch store
    "weather",              # a network tool
    "recall_british",       # facts from the scratch copy
    "vault_deadline",       # notes group
    "briefing_real",        # planner path, no model
    "study_start",          # study mode, before the brain
    "multi_tasks_timer",    # multistep plan
    "look_rewrite_this",    # desktop context
]

# Tool name -> the root module that implements it, when it isn't tools.py.
# Read from TOOL_FUNCTIONS when possible; this is the fallback.
_TOOL_MODULE_FALLBACK = {
    "get_system_status": "optimize", "list_startup_programs": "optimize",
    "search_vault": "vault_search", "set_timer": "timers", "cancel_timer": "timers",
    "list_timers": "timers", "look_at_this": "desktop_context", "get_news": "news",
}
_MUSIC = {"play_song", "pause_music", "resume_music", "skip_song", "previous_song", "current_track"}


def tool_module(name: str) -> str | None:
    try:
        import tools
        fn = tools.TOOL_FUNCTIONS.get(name)
        if fn is not None:
            return (getattr(fn, "__module__", "") or "").split(".")[-1] or None
    except Exception:
        pass
    if name in _MUSIC:
        return "spotify_control"
    return _TOOL_MODULE_FALLBACK.get(name, "tools")


def _asserted_tools(case: dict) -> set:
    e = case.get("expect", {})
    out = set()
    for k in ("any_tool_of", "all_tools_of", "none_of"):
        out |= set(e.get(k) or ())
    out |= set((case.get("fixtures") or {}).keys())
    return out


def _offered_tools(case: dict) -> set:
    """What tool_router would put in front of the model for this prompt,
    minus the core set it offers on every turn (those count only when the
    case asserts them, or every case would map to tools.py)."""
    try:
        import brain
        import tool_router
        msgs = list(case.get("before", [])) + [{"role": "user", "content": case["prompt"]}]
        names = {d["function"]["name"] for d in tool_router.select(brain.TOOL_DEFINITIONS, msgs)}
        return names - set(tool_router.CORE)
    except Exception:
        return set()


def _matches(obj, attr, text) -> bool:
    rx = getattr(obj, attr, None)
    try:
        return bool(rx.search(text)) if rx is not None else False
    except Exception:
        return False


def case_modules(case: dict) -> set:
    """Root modules this case can exercise, decided from the prompt and the
    case's own keys -- the same matchers the brain runs before the model,
    so no model is needed. Always includes the modules of the tools it
    asserts and the non-core tools the router would offer it."""
    mods = set()
    text = case.get("prompt", "")
    for name in _asserted_tools(case) | _offered_tools(case):
        m = tool_module(name)
        if m:
            mods.add(m)
            if name == "get_news":
                mods.add("news")
            if name in ("add_task", "list_tasks", "complete_task", "remove_task"):
                mods.add("tasks")
    if case.get("study") is not None:
        mods.add("study_mode")
    if case.get("vault"):
        mods |= {"vault_search", "planner", "study_brief"}
    if case.get("desktop"):
        mods.add("desktop_context")
    try:
        import brain
        if _matches(brain, "_BRIEFING", text) or _matches(brain, "_PLAN_STUDY", text) \
                or _matches(brain, "_FOLLOW_ON", text):
            mods.add("planner")
        if _matches(brain, "_STUDY_BRIEF", text):
            mods.add("study_brief")
        if _matches(brain, "_NEWS_OFF", text):
            mods.add("news")
        if _matches(brain, "_COMPLAINT", text):
            mods.add("clarify")
        if getattr(brain, "_should_delegate", lambda t: False)(text):
            mods.add("delegate")
    except Exception:
        pass
    for mod, fn in (("study_brief", "match_module"), ("study_mode", "match_command"),
                    ("multistep", "is_multistep"), ("desktop_context", "has_cue")):
        try:
            m = __import__(mod)
            if getattr(m, fn)(text):
                mods.add(mod)
        except Exception:
            pass
    if re.search(r"\bwatchlist|market brief", text, re.I):
        mods.add("invest")
    return mods


def brain_imports() -> set:
    """Root modules brain.py imports, read from its source: a change to one
    of them that no case maps to still runs the whole suite."""
    try:
        src = (ROOT / "brain.py").read_text(encoding="utf-8")
    except OSError:
        return set()
    found = set(re.findall(r"^\s*(?:from|import)\s+([a-z_][a-z0-9_]*)", src, re.M))
    return {m for m in found if (ROOT / f"{m}.py").exists()}


def changed_files(ref: str) -> list:
    """Tracked files different from `ref` in the working tree, plus
    untracked files git doesn't ignore. Repo-relative, forward slashes."""
    out = []
    for args in (["git", "diff", "--name-only", ref, "--"],
                 ["git", "ls-files", "--others", "--exclude-standard"]):
        r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(args)}: {r.stderr.strip() or 'failed'}")
        out += [ln.strip().replace("\\", "/") for ln in r.stdout.splitlines() if ln.strip()]
    return sorted(set(out))


def select_cases(cases: list, ref: str | None = None, changed: list | None = None) -> tuple:
    """(cases to run, reasons). `changed` overrides the git lookup for tests.

    Rules, in order:
      - a CORE_FILES change, or a change to a module brain imports that no
        case maps to -> every case ("all")
      - a changed root module -> the cases whose case_modules() name it
      - everything else (docs, tests, scripts, orchestra) -> no cases
      - plus SMOKE, always
    """
    if changed is None:
        changed = changed_files(ref or "HEAD")
    reasons = []
    by_id = {c["id"]: c for c in cases}
    picked = {cid for cid in SMOKE if cid in by_id}
    imports = brain_imports()
    modmap = None
    for path in changed:
        if path in CORE_FILES:
            reasons.append(f"{path}: on every turn's path -> full suite")
            return list(cases), reasons
        m = re.fullmatch(r"([a-z_][a-z0-9_]*)\.py", path)
        if not m:
            continue
        mod = m.group(1)
        if modmap is None:
            modmap = {c["id"]: case_modules(c) for c in cases}
        hits = [cid for cid, mods in modmap.items() if mod in mods]
        if hits:
            picked.update(hits)
            reasons.append(f"{path}: {len(hits)} case(s)")
        elif mod in imports:
            reasons.append(f"{path}: brain imports it and no case maps to it -> full suite")
            return list(cases), reasons
        else:
            reasons.append(f"{path}: not on the brain's path")
    chosen = [c for c in cases if c["id"] in picked]
    reasons.append(f"smoke set: {len([s for s in SMOKE if s in by_id])} case(s)")
    return chosen, reasons
