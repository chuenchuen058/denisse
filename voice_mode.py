"""
Denisse -- voice mode.

Push-to-talk style: press Enter, then speak. Denisse transcribes what you
said, sends it through the same brain as the text CLI, prints the reply,
and speaks it back out loud.

Run:
    python voice_mode.py

Say 'exit' or 'quit' (typed or spoken) to stop. Ctrl+C also works.
"""

import os
import sys
import threading

import pyttsx3

import conversation
import speech_control
import stt_vosk
import voice_pipeline
from safety import confirm_risky_tool
from voice_output import speak_inworld, TTS_ENGINE

# Local pyttsx3 fallback voice, used if the Inworld voice (see
# voice_output.py) is unset or fails -- no API key, offline, package
# missing, service hiccup. Override with a substring of a specific voice
# name (e.g. "Zira") via the DENISSE_VOICE env var / .env. Defaults to
# picking a female-sounding voice.
VOICE_PREFERENCE = os.environ.get("DENISSE_VOICE", "female")


def _select_voice(engine) -> None:
    """Point the TTS engine at a voice matching VOICE_PREFERENCE. Matches
    against the voice's reported gender first, then falls back to matching
    the preference as a substring of the voice's name/id (e.g. 'Zira')."""
    try:
        voices = engine.getProperty("voices") or []
    except Exception:
        return

    target = VOICE_PREFERENCE.strip().lower()
    if not voices or not target:
        return

    def matches(v) -> bool:
        gender = str(getattr(v, "gender", "") or "").lower()
        name = str(getattr(v, "name", "") or "").lower()
        vid = str(getattr(v, "id", "") or "").lower()
        return target in gender or target in name or target in vid

    for v in voices:
        if matches(v):
            engine.setProperty("voice", v.id)
            return
    # No match -- leave the engine's default voice in place.


_sink = None


def _speak_streaming(text: str, trace) -> bool:
    """Kokoro, sentence by sentence: the first sentence plays while the next
    is synthesized (voice_pipeline.StreamingSpeaker). False if Kokoro isn't
    usable, so speak() falls back to the whole-reply path."""
    global _sink
    if TTS_ENGINE != "kokoro":
        return False
    try:
        import tts_kokoro
        if not tts_kokoro.available():
            return False
        if _sink is None:
            _sink = voice_pipeline.PyAudioSink()
        epoch = speech_control.capture()
        barged = threading.Event()
        mic = voice_pipeline.MicBargeIn(barged).start() if voice_pipeline.BARGE_IN else None
        try:
            speaker = voice_pipeline.StreamingSpeaker(
                tts_kokoro.synthesize_samples, _sink, trace=trace,
                should_stop=lambda: barged.is_set() or speech_control.cancelled(epoch))
            ok = speaker.speak([text])
        finally:
            if mic is not None:
                mic.stop()
        if trace is not None:
            trace.extra.update(tts="kokoro-stream", sentences=len(speaker.spoken),
                               barged_in=barged.is_set())
        return ok
    except Exception as e:  # no output device, pyaudio trouble
        print(f"(streaming voice unavailable: {e})")
        return False


def speak(engine, text: str, trace=None) -> None:
    """Speak text out loud. Never lets a TTS hiccup take down the session --
    worst case, Denisse just goes text-only for that reply."""
    if not text:
        return
    if _speak_streaming(text, trace):
        return
    if speak_inworld(text):
        return
    if engine is None:
        return
    try:
        engine.say(text)
        engine.runAndWait()
    except Exception as e:
        print(f"(couldn't speak the reply out loud: {e})")


STT_ENGINE = os.environ.get("DENISSE_STT", "whisper").lower()


def listen(trace=None) -> str:
    """Records until a pause, transcribing live via the offline Vosk model
    (stt_vosk.py) -- no audio leaves the machine, no account needed.
    Returns '' if nothing understandable was heard within the timeout.

    Prints "listening..." only once the mic is actually open (via
    on_ready), not before -- with a big Vosk model that can take ~17s to
    load (see stt_vosk.preload()), printing it any earlier would have you
    talking into a mic that isn't listening yet, and the whole thing gets
    silently missed rather than just misheard."""
    def _announce_ready():
        print("listening...")

    # Whisper (stt_whisper.py) is more accurate on a whole utterance; Vosk
    # stays the fallback and keeps the hands-free wake-word path. Pick with
    # DENISSE_STT=whisper|vosk.
    if STT_ENGINE == "whisper":
        try:
            import stt_whisper
            if not stt_whisper.is_ready():
                print("(loading Whisper, one moment...)")
            kw = {"trace": trace} if trace is not None else {}
            return stt_whisper.transcribe_once(
                timeout=8, phrase_time_limit=15, on_ready=_announce_ready, **kw
            ).strip()
        except ImportError:
            pass
        except Exception as e:  # model can't load, CUDA trouble, mic error
            print(f"(Whisper unavailable, using Vosk: {e})")

    if not stt_vosk.is_ready():
        print("(loading voice model, one moment...)")

    try:
        text = stt_vosk.transcribe_once(
            timeout=8, phrase_time_limit=15, on_ready=_announce_ready
        ).strip()
        if trace is not None:
            trace.mark("stt_done")
        return text
    except stt_vosk.ModelUnavailable as e:
        print(f"({e})")
        return ""


def main() -> None:
    print("Denisse (voice mode) is starting up...")

    # Start warming the Vosk model now, in the background, so it's likely
    # already loaded by the time you press Enter to talk the first time --
    # see stt_vosk.preload()'s docstring for why this matters a lot more
    # than it sounds like it should.
    if STT_ENGINE == "whisper":
        try:
            import stt_whisper
            threading.Thread(target=stt_whisper.preload, daemon=True).start()
        except ImportError:
            threading.Thread(target=stt_vosk.preload, daemon=True).start()
    else:
        threading.Thread(target=stt_vosk.preload, daemon=True).start()
    # Kokoro's first sentence in a process is the slow one (GPU warm-up); pay
    # it now, in the background, not on the first real reply.
    if TTS_ENGINE == "kokoro":
        def _warm_kokoro():
            try:
                import tts_kokoro
                tts_kokoro.warm_up()
            except Exception:
                pass
        threading.Thread(target=_warm_kokoro, daemon=True).start()

    try:
        brain = conversation.get_brain()
    except RuntimeError as e:
        print(f"Startup error: {e}")
        sys.exit(1)

    try:
        tts_engine = pyttsx3.init()
        _select_voice(tts_engine)
    except Exception as e:
        print(f"(text-to-speech unavailable, continuing text-only: {e})")
        tts_engine = None

    messages = conversation.replay()
    print("Denisse is online. Press Enter, then speak. Ctrl+C to quit.\n")

    while True:
        try:
            input("[press Enter to talk] ")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        trace = voice_pipeline.LatencyTrace()
        transcript = listen(trace)
        if not transcript:
            print("(didn't catch that)\n")
            continue

        print(f"you> {transcript}")
        if transcript.lower() in ("exit", "quit"):
            print("Goodbye.")
            break

        turn_start = len(messages)
        messages.append({"role": "user", "content": transcript})
        try:
            reply, messages = brain.respond(messages, confirm=confirm_risky_tool)
        except Exception as e:
            print(f"Error: {e}\n")
            continue

        trace.mark("first_token")  # brain.respond() isn't streamed: the whole reply
        print(f"denisse> {reply}\n")
        # Spoken input, so the same mishearing gate the wake word uses.
        conversation.record(messages[turn_start:], source="voice")
        messages = conversation.replay()
        speak(tts_engine, reply, trace)
        trace.write()


if __name__ == "__main__":
    main()
