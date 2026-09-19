"""
Denisse -- wake-word voice mode: listens for her name.

Continuously listens on the microphone via an offline Vosk model
(stt_vosk.py) -- but only utterances that *mention Denisse's name
anywhere* get sent to the brain (not just a "hey Denisse" prefix --
"denisse", "hi denisse", "can you help me denisse" all count); everything
else is discarded on the spot, never logged, never sent further.

Runs fully on-device: unlike the old Google Web Speech API setup, no mic
audio leaves the machine just for transcription anymore (Inworld's cloud
TTS for replies is unaffected -- see voice_output.py). Opt-in, off by
default -- toggle it from the tray icon's right-click menu.

Say her name alone and she'll prompt you to continue (Siri-style,
two-phase), or say the whole thing in one breath: "hey Denisse, what time
is it" / "denisse what time is it".

Say "shush" any time she's composing or speaking a reply and she stops
immediately -- unlike everything else here, this doesn't need her name
said first, and doesn't start a new request of its own.

Keeps a short rolling buffer of recently-heard speech (RECENT_CONTEXT_
SECONDS, default 30s) so a command can reference what was just said
("Denisse, what did I just say?") even if it wasn't part of the same
utterance as her name. Deliberately narrow, not a general recording
feature: in-process memory only, never written to disk, and it ages out
on its own -- nothing here persists past that rolling window. See
brain.py's Brain.respond(extra_context=...) for how it reaches the model
without ever touching the persisted conversation history.

The mic is never blocked by a reply in progress: the listening loop only
ever transcribes and dispatches -- composing (Brain.respond) and speaking
happen in a separate thread per request, so a new mention of her name is
heard immediately even while she's still answering the last one. A newer
mention always wins: it interrupts whatever's currently being composed
or spoken (a generation counter marks older in-flight requests stale so
they quietly drop instead of speaking over the new one; in-progress
playback is cut short via a poll-and-self-stop pattern in
voice_output.PreparedAudio.play() -- Windows' MCI ties an opened audio
device to the thread that opened it, so the interrupting thread can't
just tell it to stop directly, verified directly before relying on it).

Run standalone:
    python wake_word.py
Ctrl+C to stop.
"""

import os
import queue
import re
import sys
import threading
import time
from collections import deque

import pyttsx3

import conversation
import stt_vosk
import voice_mode
from brain import Brain
from safety import confirm_risky_tool
from voice_output import synthesize_inworld, prepare_audio

# Matches her name anywhere in the transcript -- "hey denisse", "denisse",
# "can you help me denisse", "denisse what's the weather" all trigger.
# Wide on purpose: a short isolated word like "Denisse" said alone is
# genuinely hard for free speech-to-text, which frequently truncates or
# mangles it (measured, via synthesized-speech round-trips through Google:
# bare "denisse" alone often produces NO transcript via the plain API, and
# even its own suppressed alternatives were "Den", "Denn", "denne", "Denly",
# "dehn" -- none of which the original stricter pattern matched at all).
# So this matches "den" + up to 5 more letters, not just "den[iy]ss?[ae]?".
# Tradeoff: real false-positive risk from unrelated "den-" words in normal
# speech -- explicitly excluded the common ones below (deny, density,
# dennis, ...); "den" itself (a room) stays allowed since it's also the
# single most common real truncation of her name.
_NAME_RE = re.compile(r"\bden[a-z]{0,5}\b", re.IGNORECASE)
_NAME_FALSE_POSITIVES = {
    "dent", "dents", "dented", "deny", "denies", "denied", "denying",
    "dense", "denser", "density", "dental", "denote", "denoted",
    "denial", "denials", "dengue", "denmark", "denver", "dennis",
    "denny", "dentist", "denton",
}
# Strips filler immediately before her name -- "hey"/"hi"/"hello" as
# before, plus a leading "the" (found live: Vosk's decoder reliably
# hallucinates a short filler word right at the start of an utterance --
# "the hey denisse ..." -- on otherwise-correct transcripts; see
# [[Offline Speech Recognition (Vosk)]]/stt_vosk.py). Left unstripped this
# doesn't break *detection* (the name regex matches anywhere), but it does
# leak into the command text sent to the brain, and can break the bare
# "Denisse" flow specifically: "the denisse" would leave "the" as a
# nonempty leftover command, so it's read as a real (nonsense) command
# instead of the Siri-style "just her name" follow-up prompt.
_GREETING_SUFFIX_RE = re.compile(r"(?:\bthe\b\s*)?(?:hey|hi|hello)?[,]?\s*$", re.IGNORECASE)

# Vosk's own streaming endpointing decides where a phrase starts/ends
# (see stt_vosk.py) -- no fixed listen/phrase-length timeouts needed for
# the main loop anymore. This one's still meaningful: how long to wait for
# a follow-up command after a bare "Denisse" before giving up on it.
FOLLOW_UP_TIMEOUT = 6  # seconds to wait for the command after a bare wake word

# Spoken when just her name is said with nothing else attached. A longer
# phrase takes longer to say out loud (that's just its length, not lag --
# local TTS still starts near-instantly, see the local-voice note below).
WAKE_ACK = os.environ.get("DENISSE_WAKE_ACK", "Yes Chuen? How can I help you?")

# Short rolling buffer of recently-heard speech, in-process memory only --
# never written to disk, never added to the persisted conversation
# history (see brain.py's extra_context param). Entries older than this
# many seconds age out on their own read; there is no "record everything"
# mode here, deliberately.
RECENT_CONTEXT_SECONDS = int(os.environ.get("DENISSE_RECENT_CONTEXT_SECONDS", "30"))
_recent_utterances = deque()  # [(timestamp, text), ...]


def _remember_recent(text: str) -> None:
    if not text:
        return
    _recent_utterances.append((time.time(), text))
    _prune_recent()


def _prune_recent() -> None:
    cutoff = time.time() - RECENT_CONTEXT_SECONDS
    while _recent_utterances and _recent_utterances[0][0] < cutoff:
        _recent_utterances.popleft()


def _recent_context_text() -> str:
    _prune_recent()
    return " / ".join(text for _, text in _recent_utterances)


# "Newest mention of her name wins" -- see the module docstring. A single
# generation counter marks which in-flight request is current; an older
# one's worker thread checks this before ever speaking and just drops its
# result if it's been superseded. `_active_interrupt` is the currently-
# playing reply's own threading.Event, set (not called into directly --
# see PreparedAudio.play()'s own docs on why) to cut it short.
_state_lock = threading.Lock()
_generation = 0
_active_interrupt = None


def _start_new_request() -> int:
    global _generation, _active_interrupt
    with _state_lock:
        _generation += 1
        gen = _generation
        if _active_interrupt is not None:
            _active_interrupt.set()
        _active_interrupt = None
    return gen


def _is_current(gen: int) -> bool:
    with _state_lock:
        return gen == _generation


def _register_playing(event) -> None:
    global _active_interrupt
    with _state_lock:
        _active_interrupt = event


def _unregister_playing(event) -> None:
    global _active_interrupt
    with _state_lock:
        if _active_interrupt is event:
            _active_interrupt = None


def _clean_edge(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" ,.:;!?")


# A specific, observed way Vosk's general (open-vocabulary) recognizer
# mishears her name -- not a mumble of "denisse"/"denise", but split into
# two unrelated real words that happen to match the sound. Never checked
# unconditionally (see _match_wake_word's trust_fallback): "the nice" is
# common enough in ordinary sentences ("that's the nice one") that
# treating it as her name any time it appears would misfire constantly.
# Only trusted when stt_vosk.py's dedicated grammar-constrained spotter
# has *independently* confirmed her name was said in this utterance --
# see StreamingSTT's "wake_detected" event.
_MISHEARD_NAME_RE = re.compile(r"\bthe\s+nice\b", re.IGNORECASE)

# A standalone interrupt word -- unlike everything else here, this never
# needs her name said first: saying "shush" while she's mid-reply (still
# composing or actively speaking) cuts her off immediately, the same
# interrupt machinery a fresh mention of her name already uses (see
# _start_new_request), just without dispatching a new command afterward.
# Checked before the wake-word match on every finalized utterance.
_SHUSH_RE = re.compile(r"\bshush\b", re.IGNORECASE)


def _strip_wake_word(transcript: str, pattern=_NAME_RE, false_positives=_NAME_FALSE_POSITIVES):
    """If Denisse's name (per `pattern`) appears anywhere in `transcript`,
    return the command -- everything else in the transcript, with the
    name itself (and an immediately preceding bare greeting like
    "hey"/"hi") removed. Returns '' if the name was the whole utterance (a
    bare wake word). Returns None if her name isn't mentioned at all --
    caller should ignore the transcript entirely."""
    m = None
    for candidate in pattern.finditer(transcript):
        if candidate.group().lower() not in false_positives:
            m = candidate
            break
    if m is None:
        return None
    before = _GREETING_SUFFIX_RE.sub("", transcript[: m.start()])
    after = transcript[m.end():]
    # A stutter/repeat ("denisse denisse are you there") can leave a
    # second mention in `after` -- strip those too (but never a false
    # positive like "deny", which might legitimately be part of the
    # command, e.g. "denisse denisse don't deny it").
    after = pattern.sub(
        lambda c: "" if c.group().lower() not in false_positives else c.group(), after
    )
    parts = [p for p in (_clean_edge(before), _clean_edge(after)) if p]
    return " ".join(parts)


def _match_wake_word(alternatives: list[str], trust_fallback: bool = False):
    """Check *every* transcript alternative Vosk offers (not just its top
    guess -- a mangled "Denisse" is more likely to show up in a secondary
    alternative) for a mention of Denisse's name. Returns _strip_wake_word()'s
    result for whichever alternative matched first, or None if none did.

    `trust_fallback` -- pass True only when stt_vosk.py's dedicated
    spotter has independently confirmed her name was said in this same
    utterance (see run()'s handling of the "wake_detected" event). Then,
    if the name genuinely isn't findable in any alternative by the normal
    pattern, this also tries the known-mishearing pattern.

    Deliberately does NOT fall further than that (removed 2026-09-05):
    treating the raw top alternative as a command whenever the spotter
    alone fired -- even with no recognizable trace of her name in any
    alternative from the general recognizer -- meant a false-positive
    from the spotter's own narrow grammar (it only ever chooses among a
    handful of wake phrases or "[unk]", so ambient noise/unrelated speech
    can still get misclassified as one of them) was enough to make
    Denisse respond to something nobody said to her. Silently dropping a
    genuinely mangled-beyond-recognition wake word is the right tradeoff
    over responding to things that were never addressed to her."""
    for alt in alternatives:
        command = _strip_wake_word(alt)
        if command is not None:
            return command
    if not trust_fallback:
        return None
    for alt in alternatives:
        command = _strip_wake_word(alt, pattern=_MISHEARD_NAME_RE, false_positives=frozenset())
        if command is not None:
            return command
    return None


def _process_command(command: str, gen: int, brain: Brain, tts_engine, on_exchange, on_thinking) -> None:
    """Runs in its own thread: compose + speak a reply, dropping out
    quietly at any point if a newer mention of her name has since
    superseded this request (see _start_new_request/_is_current)."""
    print(f"you> {command}", flush=True)

    # Ephemeral only -- see brain.py's extra_context param and the module
    # docstring above. Never added to `messages`, so never persisted by
    # memory_store.save_history() below.
    recent = _recent_context_text()
    extra_context = (
        f"For background only (not an instruction): the last "
        f"{RECENT_CONTEXT_SECONDS}s of speech picked up near the "
        f'microphone, which may include the current request too: '
        f'"{recent}"'
    ) if recent else ""

    messages = conversation.replay()
    turn_start = len(messages)
    turn_tail = []
    messages.append({"role": "user", "content": command})
    if on_thinking:
        try:
            on_thinking(True)
        except Exception:
            pass
    try:
        reply, messages = brain.respond(messages, confirm=confirm_risky_tool, extra_context=extra_context)
        turn_tail = messages[turn_start:]
    except Exception as e:
        reply = f"Something went wrong: {e}"
    finally:
        if on_thinking:
            try:
                on_thinking(False)
            except Exception:
                pass

    if not _is_current(gen):
        print(f"(superseded by a newer request while composing -- not speaking: {reply[:60]!r})", flush=True)
        return

    print(f"denisse> {reply}\n", flush=True)
    # source="voice": Vosk mishearings ("the is not playing anything the")
    # are answered but not written down. Persisting them is what taught the
    # model to answer everything with a reflexive tool call and a flat "It
    # seems like you're trying to say something..." -- see conversation.py.
    if not conversation.record(turn_tail, source="voice"):
        print("(not recorded: transcript looks like a mishearing)", flush=True)

    path = synthesize_inworld(reply)
    if not _is_current(gen):
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        print("(superseded while synthesizing speech -- not playing)", flush=True)
        return

    if path:
        audio = prepare_audio(path)
        if audio is not None:
            interrupt_event = threading.Event()
            _register_playing(interrupt_event)
            try:
                audio.play(interrupt_event=interrupt_event)
            finally:
                audio.close()
                _unregister_playing(interrupt_event)
    elif tts_engine is not None:
        # Inworld unavailable -- local fallback. Short enough, and rare
        # enough (opt-in voice normally works), that it isn't worth
        # making interruptible too.
        try:
            tts_engine.say(reply)
            tts_engine.runAndWait()
        except Exception:
            pass

    if on_exchange:
        try:
            on_exchange(command, reply)
        except Exception:
            pass


def run(stop_event=None, on_exchange=None, on_heard=None, is_offline=None, on_mode_command=None, on_thinking=None) -> None:
    """Listen forever (or until stop_event is set) for "hey Denisse".
    `on_exchange(command, reply)`, if given, is called after each handled
    exchange -- e.g. so a caller can show a tray notification without this
    module needing to know anything about tray.py. `on_heard(text)`, if
    given, is called for *every* transcript heard, matched or not -- live
    "the mic is picking you up" feedback, not a new recording (same data
    already used for wake-word matching, just surfaced). `on_thinking(bool)`,
    if given, is called with True right before composing a reply
    (brain.respond()) and False right after -- e.g. so a caller can show a
    "composing" indicator without this module needing to know anything
    about tray.py's floating orb.

    `is_offline()`, if given, is polled after a command is matched: while
    it returns True, every command is silently discarded *except* "online
    mode" -- same idea as the exit/quit/stop-listening phrases below, just
    caller-defined so this module doesn't need to know about tray.py's
    manual-offline state itself. `on_mode_command(mode)` is called with
    "offline" or "online" when the matching phrase is heard, so the caller
    can actually flip that state; with neither passed, saying "offline
    mode"/"online mode" here is a no-op (falls through to the brain like
    any other sentence)."""
    is_offline = is_offline or (lambda: False)
    try:
        brain = conversation.get_brain()
    except RuntimeError as e:
        print(f"(wake word: brain unavailable, not starting: {e})", flush=True)
        return

    try:
        # WAKE_MODEL_PATH now follows MODEL_PATH by default (changed
        # 2026-09-05 -- see stt_vosk.py's comment on it): better accuracy
        # for hands-free commands, at the cost of stt.start() below
        # blocking for the model's full load time the first time listening
        # is turned on (this whole function runs on its own background
        # thread -- see tray.py's _start_wake_word_listening -- so that
        # delay doesn't freeze the tray icon itself, just how long it
        # takes for "Listening for Denisse..." to actually appear/be true).
        stt = stt_vosk.StreamingSTT(model_path=stt_vosk.WAKE_MODEL_PATH)
        stt.start()
    except stt_vosk.ModelUnavailable as e:
        print(f"(wake word: {e})", flush=True)
        return
    except Exception as e:
        print(f"(wake word: microphone/audio error, not starting: {e})", flush=True)
        return

    try:
        tts_engine = pyttsx3.init()
        voice_mode._select_voice(tts_engine)
    except Exception:
        tts_engine = None

    print('Listening for "Denisse"... (Ctrl+C to stop)', flush=True)

    # True while a bare "Denisse" (nothing else) has just been acknowledged
    # and the *next* finalized utterance heard should be captured as its
    # follow-up command, rather than checked against the wake-word pattern
    # again -- still one logical request, carrying the same generation.
    awaiting_followup = False
    pending_gen = None
    followup_deadline = None

    # Set by a "wake_detected" event (stt_vosk.py's dedicated
    # grammar-constrained spotter independently confirming her name was
    # said -- see _match_wake_word's trust_fallback), cleared once used or
    # once stale. Both recognizers process the same audio chunks in
    # lockstep, so the spotter's confirmation and the general recognizer's
    # "final" for the same utterance should land within a fraction of a
    # second of each other -- this window is generous, not tuned tight.
    WAKE_DETECTED_WINDOW = 3.0  # seconds
    wake_detected_at = None

    try:
        while stop_event is None or not stop_event.is_set():
            # Short poll so stop_event is still checked responsively even
            # with nothing being said.
            try:
                kind, text, alternatives = stt.events.get(timeout=0.5)
            except queue.Empty:
                if awaiting_followup and followup_deadline is not None and time.time() > followup_deadline:
                    # Gave up waiting for the follow-up command.
                    awaiting_followup = False
                    pending_gen = None
                    followup_deadline = None
                continue

            if kind == "wake_detected":
                wake_detected_at = time.time()
                continue

            if kind == "partial":
                # Live, word-by-word as it's recognized -- no waiting for a
                # full pause. Not matched against the wake word or added to
                # the rolling context buffer; only the finalized transcript
                # below is (avoids double-counting/matching on text that's
                # still being revised).
                if on_heard:
                    try:
                        on_heard(text)
                    except Exception:
                        pass
                continue

            # kind == "final"
            heard = text
            print(f"(heard: {heard!r})", flush=True)  # every transcript, matched or not -- for diagnosing missed wake words
            _remember_recent(heard)  # rolling buffer, regardless of match -- see module docs
            if on_heard:
                try:
                    on_heard(heard)
                except Exception:
                    pass

            if _SHUSH_RE.search(heard):
                # Interrupt whatever's in flight (composing or speaking --
                # _start_new_request bumps the generation so a stale
                # _process_command drops its result, and sets the active
                # playback's interrupt event so in-progress audio stops
                # too) without dispatching any new command. Also cancels a
                # pending "just said her name" follow-up wait, if one was
                # active -- "shush" isn't a command for that flow either.
                _start_new_request()
                awaiting_followup = False
                pending_gen = None
                followup_deadline = None
                continue

            if awaiting_followup:
                awaiting_followup = False
                followup_deadline = None
                command, gen = heard, pending_gen
            else:
                trust_fallback = wake_detected_at is not None and (time.time() - wake_detected_at) < WAKE_DETECTED_WINDOW
                wake_detected_at = None
                command = _match_wake_word(alternatives, trust_fallback=trust_fallback)
                if command is None:
                    continue  # not addressed to Denisse -- discard, never sent further

                # A fresh mention of her name -- this always takes priority:
                # interrupts anything currently playing/being composed (see
                # _start_new_request), even if what follows is just "Denisse"
                # alone again.
                gen = _start_new_request()

                if not command:
                    # Just her name, nothing else -- acknowledge and listen
                    # once more, Siri-style, then capture the follow-up on
                    # the *next* finalized utterance (still the mic's only
                    # listener -- a second concurrent stream isn't needed
                    # here, just a state flag) under this same generation.
                    # Local voice only, not voice_mode.speak()'s
                    # Inworld-first path -- that's a network round-trip
                    # (~1-2s) to say a few words, which is exactly the
                    # "long pause" this is for.
                    if tts_engine is not None:
                        try:
                            tts_engine.say(WAKE_ACK)
                            tts_engine.runAndWait()
                        except Exception:
                            pass
                    awaiting_followup = True
                    pending_gen = gen
                    followup_deadline = time.time() + FOLLOW_UP_TIMEOUT
                    continue

            normalized = command.strip().lower().rstrip(".!")
            if normalized == "offline mode":
                if on_mode_command:
                    on_mode_command("offline")
                voice_mode.speak(tts_engine, "Going offline.")
                continue
            if normalized == "online mode":
                if on_mode_command:
                    on_mode_command("online")
                voice_mode.speak(tts_engine, "Back online.")
                continue
            if is_offline():
                # Manually offline -- discard everything except the two
                # phrases above, which always get through so you can
                # actually say your way back online.
                continue

            if command.strip().lower() in ("exit", "quit", "stop listening"):
                voice_mode.speak(tts_engine, "Okay.")
                if stop_event is not None:
                    stop_event.set()
                break

            # Compose + speak on their own thread -- the mic loop above goes
            # straight back to listening, never blocked by a reply in
            # progress (see the module docstring and _process_command).
            threading.Thread(
                target=_process_command, args=(command, gen, brain, tts_engine, on_exchange, on_thinking), daemon=True
            ).start()
    finally:
        stt.stop()


def main() -> None:
    try:
        run()
    except KeyboardInterrupt:
        print("\nGoodbye.")
        sys.exit(0)


if __name__ == "__main__":
    main()
