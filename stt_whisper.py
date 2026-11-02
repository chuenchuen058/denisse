"""
Denisse -- offline speech-to-text with Whisper (faster-whisper), for
push-to-talk (voice_mode.py). Same contract as stt_vosk.transcribe_once.

Why alongside Vosk, not instead of it: hands-free wake-word listening
(wake_word.py) needs Vosk's live partial results and its grammar-constrained
"Denisse" spotter; Whisper transcribes a finished clip. Whisper is the more
accurate of the two on a whole utterance, so it takes the one-shot path, and
the wake-word path keeps Vosk. Pick with DENISSE_STT=whisper|vosk (default
whisper when faster-whisper is installed; any Whisper failure falls back to
Vosk in voice_mode.listen()).

Model: DENISSE_WHISPER_MODEL (default "small.en", ~480 MB, downloaded once to
the Hugging Face cache). Runs on the GPU (float16) when CUDA libraries load,
otherwise on the CPU (int8). The GPU is shared with Ollama's llama3.1:8b
(~5.6 GB of 8 GB), and small.en in float16 needs about 1 GB.
Everything stays on this machine.
"""
from __future__ import annotations

import audioop
import glob
import os
import sys
import threading
import time

import numpy as np

import stt_vosk  # reuse its mic-picking and downmix/resample (see its 48 kHz note)

SAMPLE_RATE = stt_vosk.SAMPLE_RATE  # 16 kHz, what Whisper expects
MODEL_NAME = os.environ.get("DENISSE_WHISPER_MODEL", "small.en")
DEVICE = os.environ.get("DENISSE_WHISPER_DEVICE", "auto")  # auto | cuda | cpu
CHUNK_FRAMES = 1024
CALIBRATE_S = 0.4
END_SILENCE_S = 0.9  # this much quiet after speech ends the clip
MIN_SPEECH_RMS = 300

_model = None
_model_device = None
_lock = threading.Lock()


class ModelUnavailable(RuntimeError):
    pass


def _add_cuda_dll_dirs() -> None:
    """pip's nvidia-cublas-cu12 / nvidia-cudnn-cu12 put their DLLs under
    site-packages/nvidia/*/bin, which Windows doesn't search on its own."""
    if os.name != "nt":
        return
    for base in sys.path:
        for d in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(d)
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            except OSError:
                pass


def _load(device: str):
    from faster_whisper import WhisperModel
    if device == "cuda":
        _add_cuda_dll_dirs()
        m = WhisperModel(MODEL_NAME, device="cuda", compute_type="float16")
        # CUDA libraries load lazily: a missing cuBLAS/cuDNN only shows up on
        # first use, so run a tiny clip now rather than mid-sentence later.
        list(m.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))[0])
        return m
    return WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")


def get_model():
    global _model, _model_device
    with _lock:
        if _model is not None:
            return _model
        try:
            import faster_whisper  # noqa: F401
        except ImportError as e:
            raise ModelUnavailable("faster-whisper is not installed (pip install faster-whisper)") from e
        order = ["cuda", "cpu"] if DEVICE == "auto" else [DEVICE]
        last = None
        for dev in order:
            try:
                _model, _model_device = _load(dev), dev
                return _model
            except Exception as e:  # CUDA libs missing, out of memory, ...
                last = e
        raise ModelUnavailable(f"Whisper model '{MODEL_NAME}' could not load: {last}")


def device() -> str | None:
    return _model_device


def is_ready() -> bool:
    return _model is not None


def preload() -> None:
    try:
        get_model()
    except ModelUnavailable:
        pass


def transcribe_array(audio: np.ndarray) -> str:
    """audio: float32 mono at 16 kHz in [-1, 1]."""
    segments, _info = get_model().transcribe(audio, language="en", beam_size=5, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


def transcribe_file(path: str) -> str:
    """Any audio file faster-whisper can decode (wav, mp3...)."""
    segments, _info = get_model().transcribe(path, language="en", beam_size=5, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


def _record(timeout: float, phrase_time_limit: float, on_ready) -> bytes:
    import pyaudio
    pa = pyaudio.PyAudio()
    try:
        index, native_rate, channels = stt_vosk._pick_input_device(pa)
        native_chunk = max(1, int(CHUNK_FRAMES * native_rate / SAMPLE_RATE))
        stream = pa.open(format=pyaudio.paInt16, channels=channels, rate=native_rate, input=True,
                         input_device_index=index, frames_per_buffer=native_chunk)
        state = None
        frames: list[bytes] = []
        if on_ready:
            try:
                on_ready()
            except Exception:
                pass
        chunk_s = CHUNK_FRAMES / SAMPLE_RATE
        noise, n_cal = [], max(1, int(CALIBRATE_S / chunk_s))
        started_at = None
        last_voice = None
        start_deadline = time.time() + timeout
        try:
            while True:
                raw = stream.read(native_chunk, exception_on_overflow=False)
                mono = stt_vosk._first_channel(raw, channels)
                if native_rate != SAMPLE_RATE:
                    mono, state = audioop.ratecv(mono, 2, 1, native_rate, SAMPLE_RATE, state)
                rms = audioop.rms(mono, 2)
                now = time.time()
                if len(noise) < n_cal:
                    noise.append(rms)
                    continue
                gate = max(MIN_SPEECH_RMS, 3 * (sum(noise) / len(noise)))
                loud = rms > gate
                if started_at is None:
                    frames = (frames + [mono])[-4:]  # keep ~0.25 s of lead-in
                    if loud:
                        started_at = last_voice = now
                    elif now > start_deadline:
                        return b""
                    continue
                frames.append(mono)
                if loud:
                    last_voice = now
                if now - last_voice > END_SILENCE_S or now - started_at > phrase_time_limit:
                    return b"".join(frames)
        finally:
            stream.stop_stream()
            stream.close()
    finally:
        pa.terminate()


def transcribe_once(timeout: float = 8.0, phrase_time_limit: float = 15.0, on_ready=None) -> str:
    """Blocking push-to-talk: waits up to `timeout` s for speech, records until
    END_SILENCE_S of quiet (or `phrase_time_limit`), returns the transcript or ''.
    Loads the model BEFORE opening the mic, so on_ready() means really listening."""
    get_model()
    pcm = _record(timeout, phrase_time_limit, on_ready)
    if not pcm:
        return ""
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return transcribe_array(audio)
