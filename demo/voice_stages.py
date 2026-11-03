"""Stage breakdown of real spoken turns, as JSON, for demo/run_demo.py.

The same numbers `scripts/eval_local_voice.py --breakdown` prints: p50/p95
seconds from the end of speech to each stage (STT done, first token, first
sentence, first TTS chunk, playback start), one table per path (voice_mode,
wake_word, tray_chat). Read from the turn log that voice_mode.py / the tray /
wake_word.py append to (voice_pipeline.LATENCY_LOG, or --log).

Only stage names, counts and percentiles are written; the log has no text.
With no timed turns the output has turns == 0 and run_demo.py records the
part as skipped, never as a measurement.

    .venv\\Scripts\\python.exe demo\\voice_stages.py --out logs\\voice_stages.json [--log PATH]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--log", help="turn log (default voice_pipeline.LATENCY_LOG)")
    args = ap.parse_args()

    import voice_pipeline as vp
    rows = vp.read_log(args.log)
    sources = {}
    for source in sorted({vp.row_source(r) for r in rows}):
        b = vp.breakdown(rows, source)
        if b:
            sources[source] = b
    timed = sum(1 for r in rows if r.get("deltas"))
    out = {"turns": timed, "stages": list(vp.STAGES), "sources": sources}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"{timed} timed turns; sources: {', '.join(sources) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
