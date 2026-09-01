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

import speech_recognition as sr
import pyttsx3

from brain import Brain
import memory_store

# Override with a substring of a specific voice name (e.g. "Zira") via the
# DENISSE_VOICE env var / .env. Defaults to picking a female-sounding voice.
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


def speak(engine, text: str) -> None:
    """Speak text out loud. Never lets a TTS hiccup take down the session --
    worst case, Denisse just goes text-only for that reply."""
    if not text or engine is None:
        return
    try:
        engine.say(text)
        engine.runAndWait()
    except Exception as e:
        print(f"(couldn't speak the reply out loud: {e})")


def listen(recognizer: sr.Recognizer, mic: sr.Microphone) -> str:
    """Records until a pause, then transcribes with Google's free Web
    Speech API (no key needed). Returns '' if nothing understandable
    was heard."""
    with mic as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.4)
        print("listening...")
        audio = recognizer.listen(source, timeout=8, phrase_time_limit=15)

    try:
        return recognizer.recognize_google(audio).strip()
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as e:
        print(f"(speech recognition service error: {e})")
        return ""
    except sr.WaitTimeoutError:
        return ""


def main() -> None:
    print("Denisse (voice mode) is starting up...")

    try:
        brain = Brain()
    except RuntimeError as e:
        print(f"Startup error: {e}")
        sys.exit(1)

    recognizer = sr.Recognizer()
    try:
        mic = sr.Microphone()
    except OSError as e:
        print(f"No microphone found: {e}")
        sys.exit(1)

    try:
        tts_engine = pyttsx3.init()
        _select_voice(tts_engine)
    except Exception as e:
        print(f"(text-to-speech unavailable, continuing text-only: {e})")
        tts_engine = None

    messages = memory_store.load_history()
    print("Denisse is online. Press Enter, then speak. Ctrl+C to quit.\n")

    while True:
        try:
            input("[press Enter to talk] ")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        transcript = listen(recognizer, mic)
        if not transcript:
            print("(didn't catch that)\n")
            continue

        print(f"you> {transcript}")
        if transcript.lower() in ("exit", "quit"):
            print("Goodbye.")
            break

        messages.append({"role": "user", "content": transcript})
        try:
            reply, messages = brain.respond(messages)
        except Exception as e:
            print(f"Error: {e}\n")
            continue

        print(f"denisse> {reply}\n")
        memory_store.save_history(messages)
        speak(tts_engine, reply)


if __name__ == "__main__":
    main()
