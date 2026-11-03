"""One command for Denisse's evidence pack.

    .venv\\Scripts\\python.exe demo\\run_demo.py [--rounds 3] [--skip voice ...]

Runs, each in its own process with a timeout:

  suite     eval/cases.py prompt suite, N rounds, clean condition (demo/suite_rounds.py)
  dialogue  scripts/eval_dialogue_reliability.py, N rounds, tools replaced by fixtures
  actions   scripts/eval_standing_instructions.py, N rounds, unattended agent loop
  learning  scripts/eval_adaptive_brain.py, 3 trials learning on vs off (fixed by the script)
  voice     scripts/eval_local_voice.py, Kokoro TTS -> Whisper/Vosk STT, if the models exist
  voice_stages  demo/voice_stages.py: the `eval_local_voice.py --breakdown` numbers (p50/p95
            per stage of real spoken turns) from the turn log, if there is one

The suite part is also scored by area (evidence.SUITE_AREAS): multi-step
requests, look_at_this right and wrong use, fixture-vault search, study mode.
Model-backed parts run under the shared GPU lease (orchestra.local_team.lease)
when it is available, so an Orchestra local-model job doesn't share the GPU.

and records the machine (GPU, model, Ollama version) and the git commit to
demo/results/<date>.json. Then render docs/denisse_evidence.md with
demo/report.py.

A part that cannot run (Ollama down, voice models missing, timeout, crash) is
recorded as skipped or error with the reason. Nothing is filled in. Reply
text is never written to the results, which are committed to a public repo.
"""
import argparse
import ctypes
import datetime
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "demo"))
import evidence  # noqa: E402

RESULTS = ROOT / "demo" / "results"
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA.startswith("http"):
    OLLAMA = "http://" + OLLAMA
PARTS = ("suite", "dialogue", "actions", "learning", "voice", "voice_stages")
MODEL_PARTS = {"suite", "dialogue", "actions", "learning"}


# ---- machine ---------------------------------------------------------------

def _run(cmd, timeout=30, **kw):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", **kw)
        return p.returncode, p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)


def _ollama(path, body=None, timeout=5):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _ram_gb():
    if os.name != "nt":
        return None

    class MS(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong)] + \
                   [(n, ctypes.c_ulonglong) for n in ("tot", "avail", "tpf", "apf", "tv", "av", "aev")]
    m = MS()
    m.len = ctypes.sizeof(MS)
    return round(m.tot / 2**30, 1) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)) else None


def machine(python):
    info = {"os": platform.platform(), "cpu": platform.processor() or platform.machine(),
            "ram_gb": _ram_gb(), "python": platform.python_version()}
    code, out = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                      "--format=csv,noheader"])
    info["gpu"] = out.splitlines()[0] if code == 0 and out else None
    if info["gpu"] is None:
        info["gpu_note"] = "nvidia-smi unavailable: " + evidence.redact(out)[:200]
    code, out = _run([python, "-c", "import json,brain;print(json.dumps({'model':brain.MODEL,'num_ctx':brain.NUM_CTX}))"],
                     timeout=120, cwd=ROOT)
    try:
        info.update(json.loads(out.splitlines()[-1]))
    except Exception:
        info["model_note"] = "could not import brain: " + evidence.redact(out)[-200:]
    try:
        info["ollama_version"] = _ollama("/api/version")["version"]
    except Exception as exc:
        info["ollama_version"] = None
        info["ollama_note"] = f"Ollama not reachable at {OLLAMA}: {type(exc).__name__}"
    if info.get("ollama_version") and info.get("model"):
        try:
            d = _ollama("/api/show", {"model": info["model"]}).get("details", {})
            info["model_details"] = {k: d.get(k) for k in ("family", "parameter_size", "quantization_level")}
        except Exception as exc:
            info["model_details_note"] = f"/api/show failed: {type(exc).__name__}"
    return info


def git_info():
    _, commit = _run(["git", "rev-parse", "HEAD"], cwd=ROOT)
    _, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT)
    _, status = _run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT)
    return {"commit": commit, "branch": branch, "dirty_tracked_files": bool(status)}


# ---- parts -----------------------------------------------------------------

def shown(cmd):
    """The command as it goes in the public results: no local absolute paths."""
    tmp = tempfile.gettempdir().lower()
    out = []
    for c in map(str, cmd):
        if c == sys.executable:
            out.append("python")
        elif c.lower().startswith(tmp):
            out.append("<tmp>/" + Path(c).name)
        else:
            out.append(evidence.redact(c))
    return " ".join(out)


def run_part(name, cmd, out_file, shape, timeout, env=None):
    """Run one eval as a subprocess, then read and shape its JSON output."""
    entry = {"status": "error", "command": shown(cmd), "timeout_s": timeout}
    if out_file.exists():
        out_file.unlink()
    print(f"[{name}] {entry['command']}", flush=True)
    t = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env)
        entry["exit_code"] = p.returncode
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-3:]
    except subprocess.TimeoutExpired:
        entry.update(seconds=round(time.monotonic() - t, 1),
                     reason=f"timed out after {timeout}s; no partial result kept")
        return entry
    entry["seconds"] = round(time.monotonic() - t, 1)
    try:
        data = json.loads(out_file.read_text(encoding="utf-8"))
    except Exception as exc:
        entry["reason"] = f"no readable output ({type(exc).__name__}); last output: " + \
            evidence.redact(" | ".join(tail))[:400]
        return entry
    entry.update(status="ran", **shape(data))
    return entry


def voice_missing(env):
    kokoro = Path(env.get("DENISSE_KOKORO_DIR", ROOT / "models" / "kokoro"))
    if not kokoro.is_dir() or not any(kokoro.iterdir()):
        return f"Kokoro model files not found (models/kokoro or DENISSE_KOKORO_DIR)"
    return None


def voice_log(args, env):
    return Path(args.voice_log or env.get("DENISSE_VOICE_LATENCY_LOG")
                or ROOT / "logs" / "voice_latency.jsonl")


def no_stage_turns(part):
    """A stage breakdown over zero turns is not a measurement."""
    if part.get("status") == "ran" and not part.get("summary", {}).get("turns"):
        return {"status": "skipped", "command": part.get("command"),
                "reason": "the voice turn log has no timed turns"}
    return part


def gpu_lease(wait_s=300):
    """The shared local-inference lease, or None if unavailable or still busy
    (then the run goes ahead and the results say so)."""
    try:
        from orchestra.local_team.lease import GpuLease
        lease = GpuLease.for_repo(ROOT, "demo/run_demo.py", ttl_s=4 * 3600)
        if lease.acquire(wait_s=wait_s, poll_s=5):
            return lease, {"held": True}
        return None, {"held": False, "note": f"lease busy after {wait_s}s: "
                      + evidence.redact((lease.info() or {}).get("holder", "?"))}
    except Exception as exc:
        return None, {"held": False, "note": f"lease unavailable: {type(exc).__name__}"}


def plan(args, python, tmp):
    """(name, cmd, output file, shaper) for each part."""
    r = str(args.rounds)
    return {
        "suite": ([python, "demo/suite_rounds.py", "--rounds", r, "--out", str(tmp / "suite.json")],
                  tmp / "suite.json", evidence.shape_suite),
        "dialogue": ([python, "scripts/eval_dialogue_reliability.py", "--rounds", r, "--out", str(tmp / "dialogue.json")],
                     tmp / "dialogue.json", evidence.shape_dialogue),
        "actions": ([python, "scripts/eval_standing_instructions.py", "--rounds", str(min(args.rounds, 5)),
                     "--out", str(tmp / "actions.json")], tmp / "actions.json", evidence.shape_actions),
        "learning": ([python, "scripts/eval_adaptive_brain.py"],
                     ROOT / "logs" / "adaptive_comparison.json", evidence.shape_adaptive),
        "voice": ([python, "scripts/eval_local_voice.py"],
                  ROOT / "logs" / "local_voice_eval.json", evidence.shape_voice),
        "voice_stages": ([python, "demo/voice_stages.py", "--out", str(tmp / "voice_stages.json"),
                          "--log", str(args.voice_log_path)],
                         tmp / "voice_stages.json", evidence.shape_voice_stages),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, default=3, help="rounds for suite/dialogue/actions (default 3)")
    ap.add_argument("--skip", nargs="*", default=[], choices=PARTS, help="parts not to run")
    ap.add_argument("--timeout", type=int, default=2400, help="seconds per part (default 2400)")
    ap.add_argument("--models-dir", type=Path, help="folder holding kokoro/ and vosk-model-*/ "
                    "(default: models/ in this checkout)")
    ap.add_argument("--voice-log", type=Path, help="voice turn log for voice_stages "
                    "(default DENISSE_VOICE_LATENCY_LOG or logs/voice_latency.jsonl)")
    ap.add_argument("--out", type=Path, help="results file (default demo/results/<date>.json)")
    ap.add_argument("--no-report", action="store_true", help="don't render docs/denisse_evidence.md")
    args = ap.parse_args(argv)
    if args.rounds < 1:
        ap.error("--rounds must be at least 1")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    python = sys.executable
    env = dict(os.environ)
    if args.models_dir:
        env["DENISSE_KOKORO_DIR"] = str(args.models_dir / "kokoro")
        env["DENISSE_VOSK_MODEL_PATH"] = str(args.models_dir / "vosk-model-en-us-0.22-lgraph")
    (ROOT / "logs").mkdir(exist_ok=True)
    args.voice_log_path = voice_log(args, env)

    started = datetime.datetime.now().astimezone()
    result = {"generated": started.isoformat(timespec="seconds"), "rounds": args.rounds,
              "command": evidence.redact("python demo/run_demo.py " + " ".join(argv if argv is not None else sys.argv[1:])),
              "git": git_info(), "machine": machine(python), "parts": {}}
    ollama_up = bool(result["machine"].get("ollama_version"))
    lease = None
    if ollama_up and MODEL_PARTS - set(args.skip):
        lease, result["gpu_lease"] = gpu_lease()

    try:
        with tempfile.TemporaryDirectory(prefix="denisse-demo-") as tmp:
            steps = plan(args, python, Path(tmp))
            for name in PARTS:
                if name in args.skip:
                    result["parts"][name] = {"status": "skipped", "reason": "--skip on the command line"}
                    continue
                if name in MODEL_PARTS and not ollama_up:
                    result["parts"][name] = {"status": "skipped", "reason": result["machine"]["ollama_note"]}
                    continue
                if name in ("voice", "voice_stages") and (why := voice_missing(env)):
                    result["parts"][name] = {"status": "skipped", "reason": why}
                    continue
                if name == "voice_stages" and not args.voice_log_path.is_file():
                    result["parts"][name] = {"status": "skipped", "reason":
                                             "no voice turn log on this machine (voice_mode.py, the tray "
                                             "and wake_word.py write it when spoken turns are timed)"}
                    continue
                cmd, out_file, shape = steps[name]
                result["parts"][name] = run_part(name, cmd, out_file, shape, args.timeout, env)
                if name == "voice_stages":
                    result["parts"][name] = no_stage_turns(result["parts"][name])
                part = result["parts"][name]
                print(f"[{name}] {part['status']} {part.get('summary', part.get('reason', ''))}", flush=True)
    finally:
        if lease:
            lease.release()
    result["finished"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    out = args.out or RESULTS / f"{started.date().isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    if not args.no_report:
        import report
        print(f"wrote {report.write(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
