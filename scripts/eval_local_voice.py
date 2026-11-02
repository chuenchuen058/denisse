"""Measure the local voice stack end to end, with no microphone:
Kokoro speaks each sentence to a wav, then Whisper and Vosk each transcribe it.

    .venv\\Scripts\\python.exe scripts\\eval_local_voice.py
Prints word error rate (WER) and timings per engine, and writes
logs/local_voice_eval.json. Synthetic speech is easier than a real room, so
treat the numbers as a comparison between engines, not as real-world accuracy.

    .venv\\Scripts\\python.exe scripts\\eval_local_voice.py --breakdown [--log PATH]
p50/p95 of every stage of real spoken turns, from logs/voice_latency.jsonl
(written by voice_mode.py; seconds since he stopped talking).

    .venv\\Scripts\\python.exe scripts\\eval_local_voice.py --latency [--model] [--repeat N]
Before/after on the same fixed audio: end of speech to first audio, stage by
stage, for each endpointing threshold, with Whisper's WER at each. Silent:
playback is timed by opening the device and writing zeros. --model adds
Ollama's time to first token vs full reply (streamed, no tools). Writes
logs/voice_latency_eval.json. Results: docs/voice_latency.md.
"""
import audioop
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

import tts_kokoro  # noqa: E402
import stt_whisper  # noqa: E402
import voice_pipeline as vp  # noqa: E402

SENTENCES = [
    "Remind me to email my personal tutor about the internship on Friday.",
    "What is the Fourier transform of a unit step function?",
    "Play my focus playlist on Spotify and set a timer for twenty five minutes.",
    "Add a task to finish the signals and systems coursework by Thursday evening.",
    "How many sponsors have confirmed for the Malaysian society event?",
    "Open the robotic tail design in Fusion and check the servo mounting holes.",
]


def words(t):
    return re.sub(r"[^a-z0-9 ]+", " ", t.lower().replace("-", " ")).split()


def wer(ref, hyp):
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(1, len(r))


def to16k_int16(path):
    data, rate = sf.read(path, dtype="int16")
    if data.ndim > 1:
        data = data[:, 0]
    raw = data.tobytes()
    if rate != 16000:
        raw, _ = audioop.ratecv(raw, 2, 1, rate, 16000, None)
    return raw


def vosk_transcribe(raw16):
    import vosk
    import stt_vosk
    vosk.SetLogLevel(-1)
    rec = vosk.KaldiRecognizer(stt_vosk._get_model(), 16000)
    for i in range(0, len(raw16), 8000):
        rec.AcceptWaveform(raw16[i:i + 8000])
    return json.loads(rec.FinalResult()).get("text", "")


def main():
    if not tts_kokoro.available():
        sys.exit("Kokoro model files missing in models/kokoro/")
    out = {"whisper_model": stt_whisper.MODEL_NAME, "rows": []}
    t = time.time(); tts_kokoro.preload(); out["kokoro_load_s"] = round(time.time() - t, 2)
    t = time.time(); stt_whisper.get_model(); out["whisper_load_s"] = round(time.time() - t, 2)
    out["whisper_device"] = stt_whisper.device()
    have_vosk = True
    try:
        import stt_vosk
        t = time.time(); stt_vosk._get_model(); out["vosk_load_s"] = round(time.time() - t, 2)
    except Exception as e:
        have_vosk, out["vosk_error"] = False, str(e)
    tmp = os.path.join(ROOT, "logs", "_voice_eval.wav")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    for s in SENTENCES:
        row = {"text": s}
        t = time.time(); tts_kokoro.synthesize(s, tmp); row["tts_s"] = round(time.time() - t, 2)
        row["audio_s"] = round(sf.info(tmp).duration, 2)
        raw16 = to16k_int16(tmp)
        audio = np.frombuffer(raw16, dtype=np.int16).astype(np.float32) / 32768.0
        t = time.time(); row["whisper"] = stt_whisper.transcribe_array(audio); row["whisper_s"] = round(time.time() - t, 2)
        row["whisper_wer"] = round(wer(s, row["whisper"]), 3)
        if have_vosk:
            t = time.time(); row["vosk"] = vosk_transcribe(raw16); row["vosk_s"] = round(time.time() - t, 2)
            row["vosk_wer"] = round(wer(s, row["vosk"]), 3)
        out["rows"].append(row)
        print(f"- {s}\n  whisper ({row['whisper_wer']:.0%}, {row['whisper_s']}s): {row['whisper']}"
              + (f"\n  vosk    ({row['vosk_wer']:.0%}, {row['vosk_s']}s): {row['vosk']}" if have_vosk else ""))
    os.remove(tmp)
    for eng in ("whisper", "vosk"):
        rows = [r for r in out["rows"] if f"{eng}_wer" in r]
        if rows:
            out[f"{eng}_mean_wer"] = round(sum(r[f"{eng}_wer"] for r in rows) / len(rows), 3)
            out[f"{eng}_mean_s"] = round(sum(r[f"{eng}_s"] for r in rows) / len(rows), 2)
    out["tts_mean_s"] = round(sum(r["tts_s"] for r in out["rows"]) / len(out["rows"]), 2)
    json.dump(out, open(os.path.join(ROOT, "logs", "local_voice_eval.json"), "w"), indent=2)
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=2))


# -- --breakdown ----------------------------------------------------------------

def print_breakdown(path=None):
    rows = vp.read_log(path)
    print(f"{len(rows)} turns in {path or vp.LATENCY_LOG}")
    b = vp.breakdown(rows)
    if not b:
        print("(no timed turns yet: talk to voice_mode.py first)")
        return b
    print(f"{'stage':<20}{'n':>5}{'p50 s':>9}{'p95 s':>9}")
    for stage in vp.STAGES:
        if stage in b:
            r = b[stage]
            print(f"{stage:<20}{r['n']:>5}{r['p50']:>9.3f}{r['p95']:>9.3f}")
    pauses = [r["longest_pause_s"] for r in rows if "longest_pause_s" in r]
    if pauses:
        # If this creeps up to the endpoint threshold, he's being cut off.
        print(f"longest pause inside an utterance: p95 {vp.percentile(pauses, 95):.3f}s, "
              f"max {max(pauses):.3f}s (endpoint threshold {vp.END_SILENCE_S}s)")
    return b


# -- --latency ------------------------------------------------------------------

# What Denisse might say back to each SENTENCES entry: two or three sentences,
# like her real replies. Canned so the model's run-to-run variation can't
# blur a TTS comparison (the brain is unchanged by this work; --model times it).
REPLIES = [
    "Done, I've set a reminder for Friday morning to email your personal tutor. "
    "Want me to draft the email as well?",
    "The unit step has Fourier transform one over j omega, plus pi times a delta at zero. "
    "The delta comes from the step's average value of one half.",
    "Playing your focus playlist now. I've also started a twenty five minute timer.",
    "Added: finish the signals and systems coursework, due Thursday evening. "
    "That's your third task due this week.",
    "I don't have a confirmed count in your notes yet. "
    "The last update listed three sponsors as likely. Want me to check your email?",
    "Opening the robotic tail design in Fusion. "
    "The servo mounting holes are in the base plate sketch, about two thirds down the timeline.",
]
THRESHOLDS = (0.9, 0.7, 0.6, 0.5, 0.4, 0.3)
CHUNK = stt_whisper.CHUNK_FRAMES
CHUNK_S = CHUNK / 16000


def _user_stream(raw16, rng, lead_s=0.6, tail_s=2.5, noise_rms=40.0):
    """Speech with a quiet room under it: calibration lead-in, the clip, then
    silence long enough for any threshold to fire."""
    speech = np.frombuffer(raw16, dtype=np.int16).astype(np.float64)
    voiced = np.nonzero(np.abs(speech) > 300)[0]
    lead, tail = int(lead_s * 16000), int(tail_s * 16000)
    sig = np.concatenate([np.zeros(lead), speech, np.zeros(tail)])
    sig += rng.normal(0, noise_rms, len(sig))
    true_end = (lead + (voiced[-1] if len(voiced) else len(speech))) / 16000
    return np.clip(sig, -32768, 32767).astype(np.int16), true_end


def _chunk_rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


def _longest_pause(raw16):
    """Longest quiet stretch inside the speech, in seconds -- how close the
    synthetic sentences come to tripping a short threshold."""
    x = np.frombuffer(raw16, dtype=np.int16)
    rms = [_chunk_rms(x[i * CHUNK:(i + 1) * CHUNK]) for i in range(len(x) // CHUNK)]
    loud = [i for i, r in enumerate(rms) if r > vp.MIN_SPEECH_RMS]
    if len(loud) < 2:
        return 0.0
    return max(b - a - 1 for a, b in zip(loud, loud[1:])) * CHUNK_S


def _endpoint(sig, end_s):
    """Replay `sig` through the same Endpointer and lead-in rule as
    stt_whisper._record. Returns (clip int16, detected-at seconds, verdict)."""
    ep = vp.Endpointer(chunk_s=CHUNK_S, end_silence_s=end_s, timeout=8, phrase_time_limit=15)
    frames = []
    for i in range(len(sig) // CHUNK):
        mono = sig[i * CHUNK:(i + 1) * CHUNK]
        now = (i + 1) * CHUNK_S
        calibrating = ep.calibrating()
        verdict = ep.push(_chunk_rms(mono), now)
        if calibrating:
            continue
        if verdict == "timeout":
            return np.zeros(0, np.int16), now, verdict
        if ep.started_at is None:
            frames = (frames + [mono])[-4:]
            continue
        frames.append(mono)
        if verdict:
            return np.concatenate(frames), now, verdict
    return np.concatenate(frames), len(sig) / 16000, "eof"


def _time_mci_open_play(seconds):
    """The old playback path: whole-reply wav written, MCI open + play.
    Silent (zeros of the reply's length) so the eval makes no sound."""
    import tempfile
    import voice_output
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    t = time.perf_counter()
    sf.write(path, np.zeros(int(24000 * seconds), np.float32), 24000)
    audio = voice_output.prepare_audio(path)
    if audio is None:
        return None
    voice_output._mci(f"play {audio._alias} from 0 to {audio._duration_ms}")
    dt = time.perf_counter() - t
    try:
        voice_output._mci(f"stop {audio._alias}")
    except Exception:
        pass
    audio.close()
    return round(dt, 3)


def _time_stream_open(sink):
    """The new path: open the output stream, write the lead-in + one block."""
    t = time.perf_counter()
    sink.open(24000)
    sink.write(np.zeros(int(24000 * (vp.LEAD_IN_S + vp.BLOCK_S)), np.float32))
    dt = time.perf_counter() - t
    sink.abort()
    return round(dt, 3)


def _model_times(prompt):
    import ollama
    model = os.environ.get("DENISSE_MODEL", "llama3.1:8b")
    t = time.perf_counter()
    first = first_sentence = None
    text = ""
    splitter = vp.SentenceSplitter()
    for part in ollama.chat(model=model, stream=True, keep_alive=-1,
                            options={"temperature": 0.2, "num_ctx": 8192},
                            messages=[{"role": "system", "content": "You are Denisse, a concise voice "
                                       "assistant. Reply in two or three short sentences."},
                                      {"role": "user", "content": prompt}]):
        chunk = part["message"]["content"]
        if chunk and first is None:
            first = time.perf_counter() - t
        text += chunk
        if first_sentence is None and splitter.feed(chunk):
            first_sentence = time.perf_counter() - t
    done = time.perf_counter() - t
    return {"first_token_s": round(first or done, 3), "first_sentence_s": round(first_sentence or done, 3),
            "done_s": round(done, 3), "reply": text.strip()}


def _stats(xs):
    return {"p50": round(vp.percentile(xs, 50), 3), "p95": round(vp.percentile(xs, 95), 3),
            "mean": round(sum(xs) / len(xs), 3)}


def latency_eval(with_model=False, repeat=2):
    if not tts_kokoro.available():
        sys.exit("Kokoro model files missing (models/kokoro/, or set DENISSE_KOKORO_DIR)")
    out = {"whisper_model": stt_whisper.MODEL_NAME, "chunk_s": round(CHUNK_S, 4)}
    t = time.perf_counter(); tts_kokoro.preload(); out["kokoro_load_s"] = round(time.perf_counter() - t, 2)
    for k in ("kokoro_first_call_s", "kokoro_second_call_s"):
        t = time.perf_counter(); tts_kokoro.synthesize_samples("Okay."); out[k] = round(time.perf_counter() - t, 2)
    t = time.perf_counter(); stt_whisper.get_model(); out["whisper_load_s"] = round(time.perf_counter() - t, 2)
    out["devices"] = {"kokoro": tts_kokoro.device(), "whisper": stt_whisper.device()}
    sink = vp.PyAudioSink()
    rng = np.random.default_rng(0)
    tmp = os.path.join(ROOT, "logs", "_voice_latency_eval.wav")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    rows = []
    for rep in range(repeat):
        for s, reply in zip(SENTENCES, REPLIES):
            tts_kokoro.synthesize(s, tmp)
            raw16 = to16k_int16(tmp)
            sig, true_end = _user_stream(raw16, rng)
            row = {"text": s, "rep": rep, "longest_pause_s": round(_longest_pause(raw16), 3), "endpoint": {}}
            for th in THRESHOLDS:
                clip, at, verdict = _endpoint(sig, th)
                t = time.perf_counter()
                hyp = stt_whisper.transcribe_array(clip.astype(np.float32) / 32768.0) if len(clip) else ""
                stt_s = time.perf_counter() - t
                row["endpoint"][str(th)] = {"wait_s": round(at - true_end, 3), "verdict": verdict,
                                            "stt_s": round(stt_s, 3), "wer": round(wer(s, hyp), 3), "hyp": hyp}
            first = vp.split_sentences(reply)[0]
            t = time.perf_counter(); whole = tts_kokoro.synthesize_samples(reply)
            row["tts_whole_s"] = round(time.perf_counter() - t, 3)
            t = time.perf_counter(); tts_kokoro.synthesize_samples(first)
            row["tts_first_s"] = round(time.perf_counter() - t, 3)
            row["reply_audio_s"] = round(len(whole[0]) / whole[1], 2)
            row["play_mci_s"] = _time_mci_open_play(row["reply_audio_s"])
            row["play_stream_s"] = _time_stream_open(sink)
            if with_model:
                row["model"] = _model_times(s)
            rows.append(row)
            e = row["endpoint"]
            print(f"- {s[:48]}: pause {row['longest_pause_s']}s | "
                  + " ".join(f"{k}:{v['wer']:.0%}" for k, v in e.items())
                  + f" | tts whole {row['tts_whole_s']}s first {row['tts_first_s']}s"
                  + f" | play mci {row['play_mci_s']}s stream {row['play_stream_s']}s"
                  + (f" | model 1st tok {row['model']['first_token_s']}s 1st sent "
                     f"{row['model']['first_sentence_s']}s done {row['model']['done_s']}s" if with_model else ""))
    try:
        os.remove(tmp)
    except OSError:
        pass
    sink.close()
    out["rows"] = rows
    summ = {"tts_whole_s": _stats([r["tts_whole_s"] for r in rows]),
            "tts_first_s": _stats([r["tts_first_s"] for r in rows]),
            "play_stream_s": _stats([r["play_stream_s"] for r in rows]),
            "longest_pause_s_max": max(r["longest_pause_s"] for r in rows)}
    mci = [r["play_mci_s"] for r in rows if r["play_mci_s"] is not None]
    if mci:
        summ["play_mci_s"] = _stats(mci)
    for th in THRESHOLDS:
        es = [r["endpoint"][str(th)] for r in rows]
        summ[f"endpoint_{th}"] = {"wait": _stats([e["wait_s"] for e in es]),
                                  "stt": _stats([e["stt_s"] for e in es]),
                                  "mean_wer": round(sum(e["wer"] for e in es) / len(es), 3),
                                  "clips_with_errors": sum(1 for e in es if e["wer"] > 0)}
    if with_model:
        for k in ("first_token_s", "first_sentence_s", "done_s"):
            summ[f"model_{k}"] = _stats([r["model"][k] for r in rows])
    out["summary"] = summ
    with open(os.path.join(ROOT, "logs", "voice_latency_eval.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=2))
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--breakdown", action="store_true", help="p50/p95 per stage from the turn log")
    ap.add_argument("--log", help="turn log to read (default logs/voice_latency.jsonl)")
    ap.add_argument("--latency", action="store_true", help="before/after latency on the fixed audio set")
    ap.add_argument("--model", action="store_true", help="with --latency: also time Ollama streaming")
    ap.add_argument("--repeat", type=int, default=2)
    a = ap.parse_args()
    if a.breakdown:
        print_breakdown(a.log)
    elif a.latency:
        latency_eval(with_model=a.model, repeat=a.repeat)
    else:
        main()
