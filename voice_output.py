"""
Denisse -- shared text-to-speech helper.

One place that knows how to speak a reply out loud with Inworld's neural
voice (Veronica by default), shared by voice_mode.py (the console CLI) and
tray.py (the chat popup) so the logic isn't duplicated between them.
"""

import ctypes
import os
import tempfile
import time
import wave
from ctypes import c_buffer

# Inworld's neural TTS. Unlike the rest of Denisse, this is a paid cloud API
# (a ~40-minute free trial, then billed per character) needing your own
# INWORLD_API_KEY (see .env.example), so it's opt-in: with no key set,
# speak_inworld() just returns False and the caller falls back to a local
# voice (or stays text-only, for the tray popup). Override the voice with
# DENISSE_INWORLD_VOICE (any voice name from
# inworld_tts.InworldTTS().list_voices()), or the model with
# DENISSE_INWORLD_MODEL. Default model is "inworld-tts-1.5-mini" -- measured
# ~2x faster to start speaking than "inworld-tts-2" (~1s vs ~2s generation
# time before playback can even start), at some cost to expressiveness;
# switch back via DENISSE_INWORLD_MODEL=inworld-tts-2 if quality matters
# more than the delay.
INWORLD_VOICE = os.environ.get("DENISSE_INWORLD_VOICE", "Ashley")
INWORLD_MODEL = os.environ.get("DENISSE_INWORLD_MODEL", "inworld-tts-1.5-mini")


def _safe_print(msg: str) -> None:
    # tray.py can run headless under pythonw.exe (see its autostart setup),
    # where sys.stdout is None -- print() itself would raise there.
    try:
        print(msg)
    except Exception:
        pass


def synthesize_inworld(text: str) -> str | None:
    """Generate speech audio for `text` with Inworld's neural voice and
    return a temp mp3 path -- the slow, network-bound half of speaking
    (~1-2s). Returns None on anything going wrong (no API key, offline,
    package missing, quota), so the caller can fall back to something else.
    Caller owns the returned file: play it with play_audio(), which also
    deletes it afterward."""
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

    def play(self, block: bool = True, interrupt_event=None) -> None:
        """`interrupt_event`, if given (a threading.Event), lets another
        thread cut this playback short -- checked in a short polling loop
        instead of a single sleep. The actual MCI "stop" always runs on
        *this* thread (whichever one called play()), never the thread
        that set the event: measured directly that MCI's stop command has
        the same thread-affinity requirement as open/play/close (calling
        it cross-thread raises "device is not open"), so a cross-thread
        stop has to work this way -- poll-and-self-stop, not call-in."""
        _mci(f"play {self._alias} from 0 to {self._duration_ms}")
        if not block:
            return
        duration_s = self._duration_ms / 1000.0
        if interrupt_event is None:
            time.sleep(duration_s)
            return
        step = 0.05
        elapsed = 0.0
        while elapsed < duration_s:
            if interrupt_event.is_set():
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
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 400)  # 50ms of silence
        audio = PreparedAudio(path)
        audio.play(block=True)
        audio.close()
    except Exception as e:
        _safe_print(f"(audio warm-up skipped: {e})")


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
