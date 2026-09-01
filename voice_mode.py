"""
Denisse -- voice mode.

Push-to-talk style: press Enter, then speak. Denisse transcribes what you
said, sends it through the same brain as the text CLI, prints the reply,
and speaks it back out loud.

Run:
    python voice_mode.py

Say 'exit' or 'quit' (typed or spoken) to stop. Ctrl+C also works.
"""

import sys

import speech_recognition as sr
import pyttsx3

from brain import Brain
import memory_store


def speak(engine: "pyttsx3.Engine", text: str) -> None:
    if not text:
        return
    engine.say(text)
    engine.runAndWait()


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

    tts_engine = pyttsx3.init()

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
