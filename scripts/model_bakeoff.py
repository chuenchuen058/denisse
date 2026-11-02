"""
Model bake-off: the same eval suite, k rounds, one local model at a time.

Roadmap section 3 ("a newer model: the cheapest real gain left, a download
plus three eval rounds; keep the total under ~8 GB"). This is the three eval
rounds as one command, once Chuen has pulled a candidate himself:

    .venv\\Scripts\\python.exe scripts\\model_bakeoff.py --models llama3.1:8b,qwen2.5:7b --rounds 3

It never downloads anything. A name `ollama list` doesn't show is refused
before any eval starts.

Per model it records:
  - pass^k per case: passed in ALL k rounds (and the raw pass count, k of k)
  - mean seconds per turn
  - VRAM after load: `ollama ps` SIZE, plus nvidia-smi's used/total if present
  - CPU spill: `ollama ps` PROCESSOR not "100% GPU" -- every reply slower

Only the **clean** condition runs (empty history). The live condition replays
Chuen's own conversation, which changes between runs and would make the
comparison about his history rather than the model.

Each model runs in its own child process (`--worker`), because brain.MODEL is
read from DENISSE_MODEL once at import. The child uses eval/run_eval.py's
isolate_state() and run_case(), so it touches the same scratch stores the
nightly eval does and nothing real.

Before a model is measured, every other loaded model is stopped (`ollama
stop`), so its VRAM figure and spill flag are its own. After the run any
candidate is stopped again, leaving the default model as the one in memory.

Writes docs/model_bakeoff.md (summary table + per-case diffs vs the first
model, which is the baseline) and eval/bakeoff/<stamp>.json (raw; ignored).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_MODEL = "llama3.1:8b"
DOC = ROOT / "docs" / "model_bakeoff.md"
RAW_DIR = ROOT / "eval" / "bakeoff"
VRAM_BUDGET_GB = 8.0

# Cases whose expectation is about llama3.1:8b specifically, so a candidate
# of another size fails them by design, not by being worse.
MODEL_SPECIFIC_CASES = {
    "self_parameters": "expects '8 billion' / '8b' in the reply",
}

# As published by Ollama (ollama.com/library) for the default (Q4) tag's
# download, written down from the library listing, NOT re-checked or pulled
# on this machine. Treat the sizes as approximate.
CANDIDATES = [
    ("qwen2.5:7b", "4.7 GB", "tool calling; strong on structured output"),
    ("qwen3:8b", "5.2 GB", "tool calling; has a thinking mode to keep off"),
    ("mistral:7b", "4.1 GB", "tool calling (v0.3); smaller, older"),
    ("granite3.3:8b", "4.9 GB", "IBM; tool calling, 128K context"),
    ("llama3.2:3b", "2.0 GB", "same family, much smaller; a speed floor, not a rival"),
]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --- ollama / GPU probes (pure parsing kept separate so tests can feed text)

def _run(cmd: list, timeout: float = 30) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace",
                           creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout if p.returncode == 0 else ""


def _rows(table: str) -> list[list[str]]:
    """Split `ollama list` / `ollama ps` output into rows of columns.
    Columns are separated by 2+ spaces; the header row is dropped."""
    lines = [ln for ln in (table or "").splitlines() if ln.strip()]
    return [re.split(r"\s{2,}", ln.strip()) for ln in lines[1:]]


def parse_list(text: str) -> set[str]:
    """Model names from `ollama list`."""
    return {r[0] for r in _rows(text) if r}


def parse_ps(text: str) -> list[dict]:
    """`ollama ps` rows: name, size (as printed), size_gb, processor, spilled."""
    out = []
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return out
    header = re.split(r"\s{2,}", lines[0].strip())
    idx = {h.upper(): i for i, h in enumerate(header)}
    for r in _rows(text):
        def col(name, default=""):
            i = idx.get(name)
            return r[i] if i is not None and i < len(r) else default
        size = col("SIZE")
        proc = col("PROCESSOR")
        out.append({
            "name": col("NAME", r[0] if r else ""),
            "size": size,
            "size_gb": _size_gb(size),
            "processor": proc,
            "spilled": is_spilled(proc),
        })
    return out


def _size_gb(size: str) -> float | None:
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?B)", size or "", re.I)
    if not m:
        return None
    mult = {"B": 1e-9, "KB": 1e-6, "MB": 1e-3, "GB": 1.0, "TB": 1e3}[m.group(2).upper()]
    return round(float(m.group(1)) * mult, 2)


def is_spilled(processor: str) -> bool:
    """`100% GPU` is the only fully-on-GPU answer. `48%/52% CPU/GPU` and
    `100% CPU` both mean layers on the CPU, and every reply much slower."""
    p = (processor or "").upper().replace(" ", "")
    if not p:
        return False
    return p != "100%GPU"


def parse_nvidia_smi(text: str) -> dict | None:
    """`nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits`."""
    for ln in (text or "").splitlines():
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) >= 2 and all(p.replace(".", "").isdigit() for p in parts[:2]):
            return {"used_mib": int(float(parts[0])), "total_mib": int(float(parts[1]))}
    return None


class Ollama:
    """The real CLI. Tests swap in a fake with the same four methods."""

    def installed(self) -> set[str]:
        return parse_list(_run(["ollama", "list"]))

    def loaded(self) -> list[dict]:
        return parse_ps(_run(["ollama", "ps"]))

    def stop(self, name: str) -> None:
        _run(["ollama", "stop", name], timeout=60)

    def gpu(self) -> dict | None:
        return parse_nvidia_smi(_run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                                      "--format=csv,noheader,nounits"]))


def resolve(name: str, installed: set[str]) -> str | None:
    """`llama3.1` matches `llama3.1:latest` the way ollama itself does."""
    if name in installed:
        return name
    if ":" not in name and f"{name}:latest" in installed:
        return f"{name}:latest"
    return None


def check_installed(models: list[str], installed: set[str]) -> tuple[list[str], list[str]]:
    """(resolved names, refused names). Nothing is pulled, ever."""
    ok, refused = [], []
    for m in models:
        r = resolve(m, installed)
        (ok if r else refused).append(r or m)
    return ok, refused


def refusal_message(refused: list[str], installed: set[str]) -> str:
    lines = [f"Not installed: {', '.join(refused)}. This harness never downloads a model."]
    for m in refused:
        lines.append(f"  pull it yourself first:  ollama pull {m}")
    lines.append(f"Installed now: {', '.join(sorted(installed)) or '(none)'}")
    return "\n".join(lines)


# --- aggregation

def aggregate(model: str, rounds: list[list[dict]], probe: dict | None = None) -> dict:
    """Fold k rounds of run_case() results into one model summary.

    pass^k for a case = it passed in every round. A model that passes a case
    2 of 3 times does not get it -- that's the flakiness the k rounds exist
    to expose."""
    k = len(rounds)
    cases: dict[str, dict] = {}
    secs = []
    for rnd in rounds:
        for r in rnd:
            c = cases.setdefault(r["id"], {"id": r["id"], "passes": 0, "runs": 0,
                                           "failures": [], "seconds": []})
            c["runs"] += 1
            c["passes"] += bool(r["passed"])
            c["seconds"].append(r["seconds"])
            secs.append(r["seconds"])
            for f in r.get("failures") or []:
                if f not in c["failures"]:
                    c["failures"].append(f)
    for c in cases.values():
        c["pass_k"] = c["runs"] == k and c["passes"] == k
    n = len(cases)
    pass_k = sum(c["pass_k"] for c in cases.values())
    probe = probe or {}
    return {
        "model": model,
        "rounds": k,
        "cases": n,
        "pass_k": pass_k,
        "pass_k_rate": round(pass_k / n, 3) if n else 0.0,
        "pass_any": sum(c["passes"] > 0 for c in cases.values()),
        "mean_seconds": round(sum(secs) / len(secs), 2) if secs else None,
        "vram_gb": probe.get("size_gb"),
        "processor": probe.get("processor"),
        "spilled": bool(probe.get("spilled")),
        "gpu": probe.get("gpu"),
        "per_case": dict(sorted(cases.items())),
    }


def diff_vs(base: dict, other: dict) -> dict:
    """Cases that changed pass^k between the baseline and another model."""
    b, o = base["per_case"], other["per_case"]
    ids = sorted(set(b) | set(o))
    gained = [i for i in ids if o.get(i, {}).get("pass_k") and not b.get(i, {}).get("pass_k")]
    lost = [i for i in ids if b.get(i, {}).get("pass_k") and not o.get(i, {}).get("pass_k")]
    return {"gained": gained, "lost": lost}


# --- the doc

def render(summaries: list[dict], stamp: str, raw_rel: str) -> str:
    L = [
        "# Model bake-off",
        "",
        f"Last run: {stamp}. Raw results: `{raw_rel}` (git-ignored).",
        "",
        "Generated by `scripts/model_bakeoff.py`. Re-run after pulling a candidate:",
        "",
        "```",
        ".venv\\Scripts\\python.exe scripts\\model_bakeoff.py --models "
        f"{DEFAULT_MODEL},<candidate> --rounds 3",
        "```",
        "",
        "The eval suite (`eval/cases.py`), **clean** condition only (no replayed "
        "history), run k times per model. **pass^k** counts a case only if it "
        "passed in every round. VRAM is `ollama ps` SIZE after load; *spilled* "
        "means `ollama ps` did not say `100% GPU`, so part of the model is on "
        "the CPU and every reply is much slower. The first model is the baseline.",
        "",
        "## Results",
        "",
        "| model | rounds | pass^k | passed at least once | mean s/turn | VRAM (ollama ps) | processor | spilled | GPU used / total |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        g = s.get("gpu") or {}
        gpu = f"{g['used_mib']} / {g['total_mib']} MiB" if g else "n/a"
        vram = f"{s['vram_gb']} GB" if s.get("vram_gb") is not None else "n/a"
        L.append(f"| `{s['model']}` | {s['rounds']} | {s['pass_k']}/{s['cases']} | "
                 f"{s['pass_any']}/{s['cases']} | {s['mean_seconds']} | {vram} | "
                 f"{s.get('processor') or 'n/a'} | {'**YES**' if s['spilled'] else 'no'} | {gpu} |")
    L.append("")

    over = [s["model"] for s in summaries
            if s.get("vram_gb") is not None and s["vram_gb"] > VRAM_BUDGET_GB]
    if over:
        L += [f"> Over the ~{VRAM_BUDGET_GB:g} GB budget: {', '.join(over)}.", ""]
    spilled = [s["model"] for s in summaries if s["spilled"]]
    if spilled:
        L += [f"> **CPU spill:** {', '.join(spilled)}. Its latency is not comparable; "
              "it does not fit at this context size.", ""]

    base = summaries[0] if summaries else None
    L += ["## Per-case diffs vs the baseline", ""]
    if not base or len(summaries) < 2:
        L += [f"Only one model in this run, so nothing to diff. Cases `{base['model'] if base else '?'}` "
              "did not pass in every round:", ""]
        if base:
            missed = [c for c in base["per_case"].values() if not c["pass_k"]]
            for c in missed:
                L.append(f"- `{c['id']}` ({c['passes']}/{c['runs']}): "
                         f"{'; '.join(c['failures'])[:200]}")
            if not missed:
                L.append("- none")
        L.append("")
    else:
        for s in summaries[1:]:
            d = diff_vs(base, s)
            L += [f"### `{s['model']}` vs `{base['model']}`", ""]
            L.append(f"- gained ({len(d['gained'])}): "
                     + (", ".join(f"`{i}`" for i in d["gained"]) or "none"))
            L.append(f"- lost ({len(d['lost'])}): "
                     + (", ".join(f"`{i}`" for i in d["lost"]) or "none"))
            for i in d["lost"]:
                c = s["per_case"].get(i)
                if c:
                    note = MODEL_SPECIFIC_CASES.get(i)
                    L.append(f"  - `{i}` ({c['passes']}/{c['runs']}): "
                             f"{'; '.join(c['failures'])[:200]}"
                             + (f" — model-specific case: {note}" if note else ""))
            L.append("")

    L += [
        "## Model-specific cases",
        "",
        "These expect llama3.1:8b by name or size, so a different candidate fails "
        "them by design. Discount them when comparing:",
        "",
    ]
    L += [f"- `{cid}`: {why}" for cid, why in MODEL_SPECIFIC_CASES.items()]
    L += [
        "",
        "## Candidates (published by Ollama, not verified here)",
        "",
        "7–8B-class models with tool calling that should fit the 8 GB GPU at "
        "Q4. Names and approximate download sizes are as published on "
        "ollama.com/library — **not re-checked, pulled or measured on this "
        "machine**. Loaded size "
        "at `num_ctx` 8192 is larger than the download (llama3.1:8b: 4.9 GB on "
        "disk, 5.8 GB loaded), so check the VRAM column after a real run.",
        "",
        "| model | download (published) | note |",
        "|---|---|---|",
    ]
    L += [f"| `{n}` | ~{sz} | {note} |" for n, sz, note in CANDIDATES]
    L += [
        "",
        "To try one, Chuen runs (this harness never pulls):",
        "",
        "```",
        "ollama pull qwen2.5:7b",
        f".venv\\Scripts\\python.exe scripts\\model_bakeoff.py --models {DEFAULT_MODEL},qwen2.5:7b --rounds 3",
        "```",
        "",
        "Remove a loser with `ollama rm <name>` to keep disk use down. Adopting "
        "a winner is `DENISSE_MODEL=<name>` (read in `brain.py`), then a full "
        "`eval/run_eval.py` with both conditions.",
        "",
    ]
    return "\n".join(L)


# --- worker: one model, k rounds, in a child process

def worker(model: str, rounds: int, out: Path, probe: Ollama | None = None) -> int:
    os.environ["DENISSE_MODEL"] = model
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    from eval.cases import CASES
    from eval import run_eval
    import conversation

    probe = probe or Ollama()
    scratch = run_eval.isolate_state()
    brain = conversation.get_brain()
    results, load = [], None
    for k in range(rounds):
        if k:
            run_eval.reseed_tasks(scratch)
        rnd = []
        for case in CASES:
            r = run_eval.run_case(brain, case, [])
            rnd.append(r)
            if load is None:
                # The first turn loaded the model: measure it now, while it is
                # the only thing on the GPU.
                rows = [p for p in probe.loaded() if p["name"] == model]
                load = dict(rows[0]) if rows else {}
                load["gpu"] = probe.gpu()
            print(f"[{'OK  ' if r['passed'] else 'FAIL'}] {model} r{k + 1} "
                  f"{r['id']:28} {r['seconds']:5}s  {r['failures'] or ''}", flush=True)
        results.append(rnd)
    out.write_text(json.dumps({"model": model, "rounds": results, "probe": load or {}},
                              ensure_ascii=False), encoding="utf-8")
    return 0


def _spawn_worker(model: str, rounds: int, out: Path) -> int:
    env = dict(os.environ, DENISSE_MODEL=model, PYTHONIOENCODING="utf-8")
    # Hand our std handles over explicitly: with CREATE_NO_WINDOW and no
    # handles given, the child gets a fresh hidden console and its progress
    # lines go nowhere (seen on the first real run, 2026-11-02).
    sys.stdout.flush()
    return subprocess.call([sys.executable, str(Path(__file__).resolve()), "--worker",
                            "--model", model, "--rounds", str(rounds), "--out", str(out)],
                           env=env, cwd=str(ROOT), creationflags=_NO_WINDOW,
                           stdout=sys.stdout, stderr=sys.stderr)


def bakeoff(models: list[str], rounds: int, probe=None, spawn=_spawn_worker,
            doc: Path = DOC, raw_dir: Path = RAW_DIR, now=None) -> int:
    probe = probe or Ollama()
    installed = probe.installed()
    if not installed:
        print("ollama list returned nothing -- is Ollama running?")
        return 2
    ok, refused = check_installed(models, installed)
    if refused:
        print(refusal_message(refused, installed))
        return 2

    summaries = []
    with tempfile.TemporaryDirectory(prefix="denisse-bakeoff-") as tmp:
        for m in ok:
            for other in probe.loaded():
                if other["name"] != m:
                    probe.stop(other["name"])
            out = Path(tmp) / (re.sub(r"[^\w.-]", "_", m) + ".json")
            rc = spawn(m, rounds, out)
            if not out.exists():
                print(f"{m}: worker exited {rc} without results")
                return 1
            data = json.loads(out.read_text(encoding="utf-8"))
            summaries.append(aggregate(m, data["rounds"], data.get("probe")))
            if m != DEFAULT_MODEL:
                probe.stop(m)

    now = now or datetime.datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw = raw_dir / f"{now.strftime('%Y-%m-%dT%H%M')}.json"
    raw.write_text(json.dumps({"stamp": stamp, "rounds": rounds, "models": summaries},
                              indent=1, ensure_ascii=False), encoding="utf-8")
    try:
        raw_rel = raw.relative_to(ROOT).as_posix()
    except ValueError:
        raw_rel = raw.as_posix()
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(render(summaries, stamp, raw_rel), encoding="utf-8")
    for s in summaries:
        print(f"{s['model']:20} pass^{rounds} {s['pass_k']}/{s['cases']}  "
              f"mean {s['mean_seconds']}s  vram {s['vram_gb']} GB  "
              f"{'SPILLED ' + str(s['processor']) if s['spilled'] else 'on GPU'}")
    print(doc)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare installed local models on the eval suite")
    ap.add_argument("--models", help="comma-separated; the first is the baseline")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--model", help=argparse.SUPPRESS)
    ap.add_argument("--out", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.rounds < 1:
        ap.error("--rounds must be at least 1")
    if a.worker:
        return worker(a.model, a.rounds, Path(a.out))
    if not a.models:
        ap.error("--models is required")
    models = [m.strip() for m in a.models.split(",") if m.strip()]

    lease = None
    try:
        from eval.run_eval import _gpu_lease
        lease = _gpu_lease()
    except Exception:
        pass
    try:
        return bakeoff(models, a.rounds)
    finally:
        if lease:
            lease.release()


if __name__ == "__main__":
    raise SystemExit(main())
