"""
Denisse -- shared text-to-speech helper.

One place that knows how to speak a reply out loud with Inworld's neural
voice (Veronica by default), shared by voice_mode.py (the console CLI) and
tray.py (the chat popup) so the logic isn't duplicated between them.
Engine order, voice and model names and playback timings come from
voice.json "output" (AGENTS.md Rule 0); the DENISSE_* env vars still win.
"""

import ctypes
import os
import tempfile
import threading
import time
import wave
import speech_control
import voice_config
from ctypes import c_buffer

_CFG = voice_config.section("output")

# Inworld's neural TTS. Unlike the rest of Denisse, this is a paid cloud API
# (a ~40-minute free trial, then billed per character) needing your own
# INWORLD_API_KEY (see .env.example), so it's opt-in: with no key set,
# speak_inworld() just returns False and the caller falls back to a local
# voice (or stays text-only, for the tray popup). Override the voice with
# DENISSE_INWORLD_VOICE (any voice name from
# inworld_tts.InworldTTS().list_voices()), or the model with
# DENISSE_INWORLD_MODEL; defaults in voice.json output.inworld_voice /
# inworld_model (the smaller model measured ~2x faster to start speaking,
# at some cost to expressiveness -- see voice.json output._why_tts).
INWORLD_VOICE = os.environ.get("DENISSE_INWORLD_VOICE", _CFG["inworld_voice"])
INWORLD_MODEL = os.environ.get("DENISSE_INWORLD_MODEL", _CFG["inworld_model"])
PLAYBACK_POLL_S = _CFG["playback_poll_s"]
LOCAL_POLL_S = _CFG["local_poll_s"]


def _safe_print(msg: str) -> None:
    # tray.py can run headless under pythonw.exe (see its autostart setup),
    # where sys.stdout is None -- print() itself would raise there.
    try:
        print(msg)
    except Exception:
        pass


# Local neural voice (tts_kokoro.py): free and offline. DENISSE_TTS (or
# voice.json output.tts_engine) picks the order: "kokoro" speaks with Kokoro and
# only uses Inworld if Kokoro fails; "inworld" keeps Inworld first and falls
# back to Kokoro. Either way pyttsx3 stays the last resort in the callers.
TTS_ENGINE = os.environ.get("DENISSE_TTS", _CFG["tts_engine"]).lower()


def _kokoro(text: str) -> str | None:
    try:
        import tts_kokoro
    except ImportError:
        return None
    return tts_kokoro.synthesize(text)


def synthesize_inworld(text: str) -> str | None:
    """Generate speech audio for `text` with the neural voice and return a
    temp audio path (Kokoro wav or Inworld mp3; name kept so tray.py and
    wake_word.py need no change). Returns None on anything going wrong, so
    the caller can fall back to pyttsx3. Caller owns the returned file: play
    it with play_audio(), which also deletes it afterward."""
    if not text:
        return None
    if TTS_ENGINE == "kokoro":
        path = _kokoro(text)
        if path:
            return path
    path = _synthesize_inworld_only(text)
    if path or TTS_ENGINE == "kokoro":
        return path
    return _kokoro(text)


# -- streamed local voice (tray + wake word) --
#
# With Kokoro, a reply is spoken sentence by sentence through one open
# PyAudio stream (voice_pipeline.StreamingSpeaker): sentence 1 plays while
# sentence 2 is synthesized, instead of synthesizing the whole reply to a
# wav and playing it through MCI. DENISSE_TTS_STREAM=0 (or voice.json
# output.tts_stream false) turns it off and keeps the old whole-reply path.
# Inworld always uses the old path.

_stream_lock = threading.Lock()
_stream_sink = None


def stream_enabled() -> bool:
    if TTS_ENGINE != "kokoro":
        return False
    env = os.environ.get("DENISSE_TTS_STREAM", "").strip().lower()
    if (env in ("0", "false", "no", "off")) if env else not _CFG["tts_stream"]:
        return False
    try:
        import tts_kokoro
        return bool(tts_kokoro.available())
    except Exception:
        return False


def speak_streaming(text: str, should_stop=None, on_start=None, trace=None) -> bool:
    """Speak `text` with streamed Kokoro, blocking until it has played or
    `should_stop()` turned true. `on_start()` fires once, on this thread,
    right before the first audio is written (the tray's bubble reveal).

    Returns True if this path handled the reply -- spoke it, or was stopped --
    and False if the caller should fall back to synthesize_inworld() + MCI
    (streaming off, Kokoro missing, nothing synthesized, no output device).
    Never False once on_start has fired, so a reply is never revealed or
    spoken twice.

    Replies are serialized on one lock: the PyAudio instance is shared, and
    each stream is opened, written and closed on the thread that holds it
    (the sink's single-thread rule). A newer reply only waits for an older
    one that wasn't stopped, and every new request stops the old one first
    (speech_control.stop())."""
    global _stream_sink
    if not text or not stream_enabled():
        return False
    started = threading.Event()

    def first_audio():
        started.set()
        if on_start is not None:
            on_start()

    try:
        import tts_kokoro
        import voice_pipeline
        with _stream_lock:
            if _stream_sink is None:
                _stream_sink = voice_pipeline.PyAudioSink()
            speaker = voice_pipeline.StreamingSpeaker(
                tts_kokoro.synthesize_samples, _stream_sink, trace=trace,
                should_stop=should_stop, on_start=first_audio)
            ok = bool(speaker.speak([text]) or started.is_set())
        if trace is not None and ok:
            trace.extra.update(tts="kokoro-stream", sentences=len(speaker.spoken),
                               interrupted=speaker.interrupted)
        return ok
    except Exception as e:  # no output device, pyaudio trouble
        _safe_print(f"(streaming voice unavailable: {e})")
        return started.is_set()


def _synthesize_inworld_only(text: str) -> str | None:
    """Inworld's paid cloud voice (~1-2 s, network-bound). None without a key."""
    if not text or not os.environ.get("INWORLD_API_KEY"):
        return None
    try:
        from inworld_tts import InworldTTS
    except ImportError:
        return None

    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        InworldTTS().generate(text, voice=INWORLD_VOICE, model=INWORLD_MODEL, output_file=path)
        return path
    except Exception as e:
        _safe_print(f"(Inworld voice unavailable: {e})")
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        return None


# -- low-level Windows audio playback (MCI via winmm.dll) --
#
# `playsound` (used previously) bundles four MCI calls -- open, set, status,
# play -- into one blocking call. Measured on this machine: open ~0.06-0.36s,
# and even issuing play itself ~0.22-0.53s (both numbers drop after a
# one-time "cold start" the first time any MCI command runs in the process).
# That's real, felt latency landing *after* text is already on screen if you
# call playsound() at reveal time. PreparedAudio splits it: open (the file-
# specific part) during the already-silent synthesis wait, and warm_up_audio()
# absorbs the process-wide cold-start cost once at app startup -- so by
# reveal time, only the unavoidable final `play` command remains.


def _mci(command: str) -> bytes:
    buf = c_buffer(255)
    error = ctypes.windll.winmm.mciSendStringA(command.encode(), buf, 254, 0)
    if error:
        err_buf = c_buffer(255)
        ctypes.windll.winmm.mciGetErrorStringA(error, err_buf, 254)
        raise RuntimeError(f"MCI error {error} for {command!r}: {err_buf.value.decode(errors='replace')}")
    return buf.value


class PreparedAudio:
    """An audio file already opened via MCI, ready to play with near-zero
    additional latency. Call play() (blocks until done) then close() -- or
    just close() to discard unplayed (e.g. on error)."""

    def __init__(self, path: str):
        self.path = path
        self._alias = f"denisse_{id(self)}_{int(time.time() * 1000)}"
        self._duration_ms = 0
        _mci(f'open "{path}" alias {self._alias}')
        try:
            _mci(f"set {self._alias} time format milliseconds")
            self._duration_ms = int(_mci(f"status {self._alias} length") or b"0")
        except Exception:
            pass  # play() below still works even without a known duration

    def play(self, block: bool = True, interrupt_event=None, speech_epoch=None) -> None:
        """`interrupt_event`, if given (a threading.Event), lets another
        thread cut this playback short -- checked in a short polling loop
        instead of a single sleep. The actual MCI "stop" always runs on
        *this* thread (whichever one called play()), never the thread
        that set the event: measured directly that MCI's stop command has
        the same thread-affinity requirement as open/play/close (calling
        it cross-thread raises "device is not open"), so a cross-thread
        stop has to work this way -- poll-and-self-stop, not call-in."""
        if speech_epoch is None:
            speech_epoch = speech_control.capture()
        def cancelled():
            return speech_control.cancelled(speech_epoch) or (
                interrupt_event is not None and interrupt_event.is_set())
        if cancelled():
            return  # a stopped pending reply must never start playing
        _mci(f"play {self._alias} from 0 to {self._duration_ms}")
        if not block:
            return
        duration_s = self._duration_ms / 1000.0
        step = PLAYBACK_POLL_S
        elapsed = 0.0
        while elapsed < duration_s:
            if cancelled():
                try:
                    _mci(f"stop {self._alias}")
                except Exception:
                    pass
                return
            time.sleep(step)
            elapsed += step

    def close(self) -> None:
        try:
            _mci(f"close {self._alias}")
        except Exception:
            pass
        try:
            os.remove(self.path)
        except OSError:
            pass


def prepare_audio(path: str) -> "PreparedAudio | None":
    """Open (but don't yet play) an audio file for near-instant playback
    later. Returns None if MCI can't open it (caller should still clean up
    the file itself in that case)."""
    try:
        return PreparedAudio(path)
    except Exception as e:
        _safe_print(f"(couldn't prepare audio: {e})")
        return None


def speak_local(engine, text, speech_epoch=None):
    """Keep local fallback speech stoppable on its own engine thread."""
    if not text or engine is None:
        return
    if speech_epoch is None:
        speech_epoch = speech_control.capture()
    if speech_control.cancelled(speech_epoch):
        return
    started = False
    try:
        engine.say(text)
        engine.startLoop(False)
        started = True
        while engine.isBusy():
            if speech_control.cancelled(speech_epoch):
                engine.stop()
                break
            engine.iterate()
            time.sleep(LOCAL_POLL_S)
    finally:
        if started:
            engine.endLoop()


def warm_up_audio() -> None:
    """Absorb Windows' one-time MCI cold-start cost (measured ~0.6s extra
    on the very first call in a process) by doing a throwaway silent
    open+play+close now, so the first *real* reply doesn't pay for it.
    Uses a tiny local silent WAV -- no network/API cost."""
    try:
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(_CFG["warm_up_rate"])
            w.writeframes(b"\x00\x00" * _CFG["warm_up_frames"])  # a moment of silence
        audio = PreparedAudio(path)
        audio.play(block=True)
        audio.close()
    except Exception as e:
        _safe_print(f"(audio warm-up skipped: {e})")
    # Kokoro's first sentence in a process takes ~5-6 s on the GPU (CUDA
    # warm-up; ~2 min the very first time on a new GPU while kernels compile),
    # then ~1 s. Pay that here, at startup, not on the first real reply.
    if TTS_ENGINE == "kokoro":
        try:
            import tts_kokoro
            tts_kokoro.warm_up()
        except Exception as e:
            _safe_print(f"(Kokoro warm-up skipped: {e})")


def play_audio(path: str) -> None:
    """Open and play an audio file in one call, then delete it. For
    callers that don't need to split open/play timing (voice_mode.py);
    tray.py uses prepare_audio()/PreparedAudio.play() instead so the
    open-overhead can happen earlier, during an already-silent wait."""
    audio = prepare_audio(path)
    if audio is None:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    try:
        audio.play()
    finally:
        audio.close()


def speak_inworld(text: str) -> bool:
    """Synthesize and play `text` with Inworld's neural voice, blocking
    until both are done. Returns True if it actually spoke, False if
    synthesis failed (see synthesize_inworld) so the caller can fall back
    to something else. For callers that want to do something (like reveal
    a chat bubble) at the moment playback *starts* rather than after it's
    all done, use synthesize_inworld() + prepare_audio() + .play() directly
    instead."""
    path = synthesize_inworld(text)
    if not path:
        return False
    play_audio(path)
    return True
