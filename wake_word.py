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

The words it listens for (her name and its known mishearings, greetings,
yes/no answers, mode and exit phrases), what she says back, and the
timeouts are all in voice.json "wake_word" and "confirm" (AGENTS.md Rule 0).

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
import study_mode
import voice_config
import voice_mode
import voice_pipeline
from brain import Brain
from safety import confirm_risky_tool
from voice_output import synthesize_inworld, prepare_audio, speak_streaming

_CFG = voice_config.section("wake_word")
_CONFIRM = voice_config.section("confirm")


def _phrase_alt(phrases, prefix_star: bool = False) -> str | None:
    """A regex alternative over literal phrases (whitespace between words
    matches any whitespace); with prefix_star, 'confirm*' also matches any
    ending. None for an empty list."""
    parts = []
    for p in phrases:
        p = " ".join(str(p).split())
        if not p:
            continue
        star = prefix_star and p.endswith("*")
        body = r"\s+".join(re.escape(w) for w in p.rstrip("*").split())
        if body:
            parts.append(body + (r"\w*" if star else ""))
    return "(?:" + "|".join(parts) + ")" if parts else None


_NEVER = re.compile(r"(?!)")  # an empty word list matches nothing

# Matches her name anywhere in the transcript -- "hey denisse", "denisse",
# "can you help me denisse", "denisse what's the weather" all trigger.
# Wide on purpose: name_stem + up to name_extra_letters more letters, since
# free STT truncates and mangles a bare "Denisse" ("Den", "Denn", "denne",
# "Denly"); the common real words that also fit are excluded by
# name_false_positives. Why, in full: voice.json wake_word._why_name.
_NAME_RE = re.compile(
    rf"\b{re.escape(_CFG['name_stem'])}[a-z]{{0,{_CFG['name_extra_letters']}}}\b", re.IGNORECASE)
_NAME_FALSE_POSITIVES = {w.lower() for w in _CFG["name_false_positives"]}
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
_fillers = _phrase_alt(_CFG["leading_fillers"])
_greetings = _phrase_alt(_CFG["greetings"])
_GREETING_SUFFIX_RE = re.compile(
    (rf"(?:\b{_fillers}\b\s*)?" if _fillers else "") + (f"{_greetings}?" if _greetings else "")
    + r"[,]?\s*$", re.IGNORECASE)

# Vosk's own streaming endpointing decides where a phrase starts/ends
# (see stt_vosk.py) -- no fixed listen/phrase-length timeouts needed for
# the main loop anymore. This one's still meaningful: how long to wait for
# a follow-up command after a bare "Denisse" before giving up on it.
FOLLOW_UP_TIMEOUT = _CFG["follow_up_timeout_s"]  # wait for the command after a bare wake word

# Spoken when just her name is said with nothing else attached. A longer
# phrase takes longer to say out loud (that's just its length, not lag --
# local TTS still starts near-instantly, see the local-voice note below).
WAKE_ACK = os.environ.get("DENISSE_WAKE_ACK", _CFG["wake_ack"])

# Short rolling buffer of recently-heard speech, in-process memory only --
# never written to disk, never added to the persisted conversation
# history (see brain.py's extra_context param). Entries older than this
# many seconds age out on their own read; there is no "record everything"
# mode here, deliberately.
RECENT_CONTEXT_SECONDS = int(os.environ.get("DENISSE_RECENT_CONTEXT_SECONDS", _CFG["recent_context_seconds"]))
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


def interrupt():
    """Quiet the current voice request and all in-process reply playback."""
    _start_new_request()
    _cancel_pending_confirm()


def _start_new_request() -> int:
    global _generation, _active_interrupt
    import speech_control
    speech_control.stop()
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
_misheard = _phrase_alt(_CFG["misheard_names"])
_MISHEARD_NAME_RE = re.compile(rf"\b{_misheard}\b", re.IGNORECASE) if _misheard else _NEVER

# A standalone interrupt word -- unlike everything else here, this never
# needs her name said first: saying "shush" while she's mid-reply (still
# composing or actively speaking) cuts her off immediately, the same
# interrupt machinery a fresh mention of her name already uses (see
# _start_new_request), just without dispatching a new command afterward.
# Checked before the wake-word match on every finalized utterance.


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


# --- Spoken confirmation ----------------------------------------------------
# Hands-free mode hears everything in the room, so a misheard phrase can turn
# into a real shell command. Typed requests in the popup are deliberate and
# stay un-gated (safety.py: run_shell_command/write_file opened up on
# 2026-09-02, on request) -- but here, those two plus anything in RISKY_TOOLS
# are asked about out loud first, and answered by voice, no name needed.
#
# It also replaces the old behaviour for RISKY_TOOLS in this mode, which was
# safety.confirm_risky_tool's input() prompt -- unanswerable with no console.
#
# DENISSE_VOICE_CONFIRM=0 turns the extra gating off (RISKY_TOOLS are still
# asked about, by voice).
import safety  # noqa: E402

VOICE_CONFIRM_ENABLED = os.environ.get("DENISSE_VOICE_CONFIRM", "1").lower() not in (
    "0", "false", "off", "no")
VOICE_CONFIRM_TOOLS = set(safety.RISKY_TOOLS) | set(_CONFIRM["extra_tools"])
CONFIRM_TIMEOUT = _CONFIRM["timeout_s"]  # silence means no
ANSWER_MAX_WORDS = _CONFIRM["answer_max_words"]


def _word_re(words):
    alt = _phrase_alt(words, prefix_star=True)
    return re.compile(rf"\b{alt}\b", re.I) if alt else _NEVER


_YES_RE = _word_re(_CONFIRM["yes_words"])
_NO_RE = _word_re(_CONFIRM["no_words"])


class _PendingConfirm:
    def __init__(self, gen: int):
        self.gen = gen
        self.answer = None
        self.event = threading.Event()

    def resolve(self, answer: bool) -> None:
        if not self.event.is_set():
            self.answer = answer
            self.event.set()


_pending_lock = threading.Lock()
_pending_confirm = None


def _classify_answer(text: str):
    """True for a yes, False for a no, None if it isn't a clear answer.

    A long sentence isn't an answer, and one with BOTH a yes-word and a
    no-word isn't either -- which matters, because the question she has
    just asked ends "say yes or no", and the mic can pick up her own voice.
    """
    t = (text or "").strip()
    if not t or len(t.split()) > ANSWER_MAX_WORDS:
        return None
    yes, no = bool(_YES_RE.search(t)), bool(_NO_RE.search(t))
    if yes == no:
        return None
    return yes


class _Fields(dict):
    """format_map fields for a describe template: a missing one is its
    arg_fallbacks entry, or empty."""

    def __missing__(self, key):
        return _CONFIRM["arg_fallbacks"].get(key, "")


def _describe_action(name: str, args: dict) -> str:
    """How the tool call is said in the spoken question (voice.json confirm.describe)."""
    limit = _CONFIRM["arg_max_chars"]
    args = args or {}
    fields = _Fields()
    for key, value in args.items():
        text = " ".join(str(value).split())
        if text:
            fields[key] = text[:limit] + ("..." if len(text) > limit else "")
    path_name = os.path.basename(str(args.get("path", "")))
    if path_name:
        fields["file"] = path_name
    fields["tool"] = name
    templates = _CONFIRM["describe"]
    template = templates.get(name) or templates.get("_default") or "{tool}"
    try:
        return template.format_map(fields)
    except (ValueError, IndexError, AttributeError, KeyError):  # a malformed template in voice.json
        return name


def _say_local(tts_engine, text: str) -> None:
    print(f"(asking: {text})", flush=True)
    if tts_engine is None:
        return
    try:
        tts_engine.say(text)
        tts_engine.runAndWait()
    except Exception:
        pass


def _make_voice_confirm(tts_engine, gen: int):
    """The confirm callback brain.respond() calls before each tool."""
    gated = VOICE_CONFIRM_TOOLS if VOICE_CONFIRM_ENABLED else set(safety.RISKY_TOOLS)

    def confirm(name: str, args: dict) -> bool:
        global _pending_confirm
        if name not in gated:
            return True
        if not _is_current(gen):
            return False
        _say_local(tts_engine, _CONFIRM["question"].replace("{action}", _describe_action(name, args)))
        pending = _PendingConfirm(gen)
        with _pending_lock:
            if _pending_confirm is not None:
                _pending_confirm.resolve(False)
            _pending_confirm = pending
        answered = pending.event.wait(CONFIRM_TIMEOUT)
        with _pending_lock:
            if _pending_confirm is pending:
                _pending_confirm = None
        if not answered:
            _say_local(tts_engine, _CONFIRM["no_answer"])
            return False
        return bool(pending.answer)

    return confirm


def _offer_answer(heard: str, alternatives=None) -> bool:
    """Called by the mic loop with every finalized utterance. Returns True
    if the utterance was consumed as a yes/no answer (or as noise while a
    question is open) and must not be treated as anything else."""
    with _pending_lock:
        pending = _pending_confirm
    if pending is None:
        return False
    answer = _classify_answer(heard)
    if answer is not None:
        pending.resolve(answer)
        return True
    # Not a yes/no. A fresh "Denisse, ..." request moves on (and declines
    # the open question); anything else is ignored while it's open.
    if _match_wake_word(alternatives or [heard]) is not None:
        pending.resolve(False)
        return False
    return True


def _cancel_pending_confirm() -> None:
    with _pending_lock:
        if _pending_confirm is not None:
            _pending_confirm.resolve(False)


def _process_command(command: str, gen: int, brain: Brain, tts_engine, on_exchange, on_thinking=None, on_reply=None) -> None:
    """Runs in its own thread: compose + speak a reply, dropping out
    quietly at any point if a newer mention of her name has since
    superseded this request (see _start_new_request/_is_current)."""
    import speech_control
    if speech_control.is_stop_request(command):
        interrupt()
        return
    speech_epoch = speech_control.capture()
    # Vosk's own endpointing has already fired and its transcript is final
    # by the time a command gets here, so "speech_end" and "stt_done" are
    # the same instant (give or take this thread's start-up).
    trace = voice_pipeline.LatencyTrace()
    trace.mark("speech_end")
    trace.mark("stt_done", trace.marks["speech_end"])
    trace.extra["source"] = "wake_word"
    print(f"you> {command}", flush=True)

    # Ephemeral only -- see brain.py's extra_context param and the module
    # docstring above. Never added to `messages`, so never persisted by
    # memory_store.save_history() below.
    recent = _recent_context_text()
    extra_context = (
        _CFG["recent_context_prompt"].replace("{seconds}", str(RECENT_CONTEXT_SECONDS))
        .replace("{recent}", recent)
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

    thinking_stopped = []

    def _stop_thinking():
        if thinking_stopped:
            return
        thinking_stopped.append(True)
        if on_thinking:
            try:
                on_thinking(False)
            except Exception:
                pass

    revealed = []

    def _reveal():
        """The reply text appears (and the thinking bubble goes) as she
        starts talking -- once, whichever voice path gets there."""
        if revealed:
            return
        revealed.append(True)
        if on_reply:
            try:
                on_reply(reply)
            except Exception:
                pass
        _stop_thinking()

    # The thinking indicator now stays up until she starts speaking, not
    # just until the reply is composed: on_reply (tray.py turns the "• • •"
    # bubble into the reply text) fires right before playback, so the text
    # appears as she talks (asked 2026-10-14). The finally covers every
    # early return and error on the way there.
    reply = ""
    path = None
    streamed = False
    try:
        try:
            reply, messages = brain.respond(messages, confirm=_make_voice_confirm(tts_engine, gen), extra_context=extra_context)
            turn_tail = messages[turn_start:]
        except Exception as e:
            reply = f"Something went wrong: {e}"

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
        trace.mark("first_token")  # brain.respond() isn't streamed: the whole reply

        # Local Kokoro: sentence by sentence through one open stream, the
        # text revealed at first audio. A newer request (generation), the
        # stop word / stop button (speech_control) and this reply's own
        # interrupt event all stop it between ~50 ms blocks.
        interrupt_event = threading.Event()
        _register_playing(interrupt_event)
        try:
            streamed = speak_streaming(
                reply,
                should_stop=lambda: (interrupt_event.is_set() or speech_control.cancelled(speech_epoch)
                                     or not _is_current(gen)),
                on_start=_reveal, trace=trace)
        finally:
            _unregister_playing(interrupt_event)
        if streamed:
            if not _is_current(gen):
                trace.write()
                print("(superseded while speaking -- stopped)", flush=True)
                return
            if not revealed:
                _reveal()  # stopped before the first sentence was ready
        else:
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
                trace.mark("tts_first_chunk")
            _reveal()
    finally:
        _stop_thinking()

    if streamed:
        trace.write()
    elif path:
        audio = prepare_audio(path)
        if audio is not None:
            interrupt_event = threading.Event()
            _register_playing(interrupt_event)
            try:
                if not speech_control.cancelled(speech_epoch):
                    trace.mark("playback_start")
                trace.extra["tts"] = "mci"
                audio.play(interrupt_event=interrupt_event, speech_epoch=speech_epoch)
            finally:
                audio.close()
                _unregister_playing(interrupt_event)
                trace.write()
    elif tts_engine is not None:
        try:
            from voice_output import speak_local
            speak_local(tts_engine, reply, speech_epoch=speech_epoch)
        except Exception:
            pass

    if on_exchange:
        try:
            on_exchange(command, reply)
        except Exception:
            pass


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def _mode_command(normalized: str):
    """The mode ("offline" / "online") whose phrase the whole command is
    (voice.json wake_word.mode_phrases), else None."""
    said = _norm(normalized)
    for mode, phrases in _CFG["mode_phrases"].items():
        if said in {_norm(p) for p in phrases}:
            return mode
    return None


def _is_exit(command: str) -> bool:
    return _norm(command) in {_norm(p) for p in _CFG["exit_phrases"]}


def run(stop_event=None, on_exchange=None, on_heard=None, is_offline=None, on_mode_command=None, on_thinking=None, on_reply=None) -> None:
    """Listen forever (or until stop_event is set) for "hey Denisse".
    `on_exchange(command, reply)`, if given, is called after each handled
    exchange -- e.g. so a caller can show a tray notification without this
    module needing to know anything about tray.py. `on_heard(text)`, if
    given, is called for *every* transcript heard, matched or not -- live
    "the mic is picking you up" feedback, not a new recording (same data
    already used for wake-word matching, just surfaced). `on_thinking(bool)`,
    if given, is called with True right before composing a reply
    (brain.respond()) and False once she starts speaking -- tray.py uses it
    for the thinking bubble out of the orb (reminder_card.ThinkingBubble).
    `on_reply(reply)`, if given, is called just before playback starts, so
    the reply text can appear while she says it.

    `is_offline()`, if given, is polled after a command is matched: while
    it returns True, every command is silently discarded *except* "online
    mode" -- same idea as the exit/quit/stop-listening phrases below, just
    caller-defined so this module doesn't need to know about tray.py's
    manual-offline state itself. `on_mode_command(mode)` is called with
    "offline" or "online" when the matching phrase is heard ("study" /
    "stop_study" for study mode), so the caller
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
    WAKE_DETECTED_WINDOW = _CFG["wake_detected_window_s"]
    wake_detected_at = None

    try:
        while stop_event is None or not stop_event.is_set():
            # Short poll so stop_event is still checked responsively even
            # with nothing being said.
            try:
                kind, text, alternatives = stt.events.get(timeout=_CFG["events_poll_s"])
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

            import speech_control
            if speech_control.is_stop_request(heard):
                # Interrupt whatever's in flight (composing or speaking --
                # _start_new_request bumps the generation so a stale
                # _process_command drops its result, and sets the active
                # playback's interrupt event so in-progress audio stops
                # too) without dispatching any new command. Also cancels a
                # pending "just said her name" follow-up wait, if one was
                # active -- "shush" isn't a command for that flow either.
                interrupt()
                awaiting_followup = False
                pending_gen = None
                followup_deadline = None
                continue

            # A spoken "should I run that?" is open: this utterance is its
            # answer (no name needed), unless it's a fresh "Denisse, ...".
            if _offer_answer(heard, alternatives):
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
            mode = _mode_command(normalized)
            if mode == "offline":
                if on_mode_command:
                    on_mode_command("offline")
                voice_mode.speak(tts_engine, _CFG["say_offline"])
                continue
            if mode == "online":
                if on_mode_command:
                    on_mode_command("online")
                voice_mode.speak(tts_engine, _CFG["say_online"])
                continue
            study = study_mode.match_command(normalized)
            if study == "study":
                if on_mode_command:
                    on_mode_command("study")
                voice_mode.speak(tts_engine, _CFG["say_study_on"])
                continue
            if study == "stop_study":
                if on_mode_command:
                    on_mode_command("stop_study")
                voice_mode.speak(tts_engine, _CFG["say_study_off"])
                continue
            if is_offline():
                # Manually offline -- discard everything except the two
                # phrases above, which always get through so you can
                # actually say your way back online.
                continue

            if _is_exit(command):
                voice_mode.speak(tts_engine, _CFG["say_exit"])
                if stop_event is not None:
                    stop_event.set()
                break

            # Compose + speak on their own thread -- the mic loop above goes
            # straight back to listening, never blocked by a reply in
            # progress (see the module docstring and _process_command).
            threading.Thread(
                target=_process_command, args=(command, gen, brain, tts_engine, on_exchange, on_thinking, on_reply), daemon=True
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
