"""Measure the local voice stack end to end, with no microphone:
Kokoro speaks each sentence to a wav, then Whisper and Vosk each transcribe it.

    .venv\\Scripts\\python.exe scripts\\eval_local_voice.py
Prints word error rate (WER) and timings per engine, and writes
logs/local_voice_eval.json. Synthetic speech is easier than a real room, so
treat the numbers as a comparison between engines, not as real-world accuracy.
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


if __name__ == "__main__":
    main()
