"""
Denisse -- local neural voice with Kokoro (82M parameters, ONNX, CPU).

Free and offline, unlike Inworld (a paid cloud API, see voice_output.py).
voice_output.synthesize_inworld() tries this first when DENISSE_TTS=kokoro
(the default once the model files exist), and also falls back to it when
Inworld is unavailable, before the robotic pyttsx3 voice.

Model files (git-ignored, ~330 MB) live in models/kokoro/:
  kokoro-v1.0.onnx, voices-v1.0.bin
from https://github.com/thewh1teagle/kokoro-onnx/releases (model-files-v1.0).
Voice: DENISSE_KOKORO_VOICE (default af_heart; any voice in voices-v1.0.bin),
speed: DENISSE_KOKORO_SPEED (default 1.0).
"""
from __future__ import annotations

import os
import tempfile
import threading

_ROOT = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.environ.get("DENISSE_KOKORO_DIR", os.path.join(_ROOT, "models", "kokoro"))
MODEL_PATH = os.path.join(MODEL_DIR, "kokoro-v1.0.onnx")
VOICES_PATH = os.path.join(MODEL_DIR, "voices-v1.0.bin")
VOICE = os.environ.get("DENISSE_KOKORO_VOICE", "af_heart")
SPEED = float(os.environ.get("DENISSE_KOKORO_SPEED", "1.0"))

_kokoro = None
_lock = threading.Lock()


def available() -> bool:
    if not (os.path.exists(MODEL_PATH) and os.path.exists(VOICES_PATH)):
        return False
    try:
        import kokoro_onnx  # noqa: F401
        import soundfile  # noqa: F401
    except ImportError:
        return False
    return True


DEVICE = os.environ.get("DENISSE_KOKORO_DEVICE", "auto")  # auto | cuda | cpu
_device = None


def _get():
    """GPU (onnxruntime-gpu's CUDA provider) when it loads, else CPU. Measured
    31 Oct 2026 on this laptop: CPU ~6.4 s per sentence, slower than the
    sentence itself takes to say."""
    global _kokoro, _device
    with _lock:
        if _kokoro is None:
            from kokoro_onnx import Kokoro
            if DEVICE != "cpu":
                try:
                    import onnxruntime as ort
                    import stt_whisper  # shares the CUDA DLL lookup
                    stt_whisper._add_cuda_dll_dirs()
                    if "CUDAExecutionProvider" in ort.get_available_providers():
                        sess = ort.InferenceSession(
                            MODEL_PATH, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
                        if sess.get_providers()[0] == "CUDAExecutionProvider":
                            _kokoro, _device = Kokoro.from_session(sess, VOICES_PATH), "cuda"
                except Exception:
                    _kokoro = None
            if _kokoro is None:
                _kokoro, _device = Kokoro(MODEL_PATH, VOICES_PATH), "cpu"
        return _kokoro


def device() -> str | None:
    return _device


def preload() -> None:
    if available():
        try:
            _get()
        except Exception:
            pass


def warm_up() -> None:
    """Load the model and speak one throwaway word, so the first real reply
    doesn't pay the GPU's warm-up."""
    path = synthesize("Ready.")
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def synthesize_samples(text: str):
    """(float32 mono samples, sample rate) for `text`, or None -- the
    no-file form, for voice_pipeline.StreamingSpeaker's one open stream."""
    if not text or not available():
        return None
    try:
        return _get().create(text, voice=VOICE, speed=SPEED, lang="en-us")
    except Exception as e:
        try:
            print(f"(Kokoro voice unavailable: {e})")
        except Exception:
            pass
        return None


def synthesize(text: str, path: str | None = None) -> str | None:
    """Speak `text` into a wav file and return its path (caller owns it), or
    None if Kokoro isn't installed or fails, so the caller can fall back."""
    if not text or not available():
        return None
    try:
        import soundfile as sf
        samples, rate = _get().create(text, voice=VOICE, speed=SPEED, lang="en-us")
        if path is None:
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
        sf.write(path, samples, rate)
        return path
    except Exception as e:
        try:
            print(f"(Kokoro voice unavailable: {e})")
        except Exception:
            pass
        return None
