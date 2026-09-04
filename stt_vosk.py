"""
Denisse -- offline streaming speech-to-text, via Vosk.

Replaces the old recognize_google() setup (SpeechRecognition + Google's
free Web Speech API): that was strictly request/response -- record a whole
phrase, *then* send it off and wait for one transcript back, so there was
no way to show what she'd heard until after you stopped talking, and no
custom vocabulary meant short/uncommon words (her own name chief among
them) were guessed at by a generic model tuned for common English.

Vosk runs entirely on this machine and streams results as audio comes in:
AcceptWaveform() returns partial text several times a second while you're
still speaking (what makes the chat popup's "heard" indicator update live,
word by word, instead of only after a full pause) and a finalized
transcript once its own endpointing detects an utterance boundary. As a
side effect, mic audio for speech recognition no longer leaves the machine
at all (Inworld's cloud TTS for replies, and Ollama's local LLM, are
unaffected -- see voice_output.py / brain.py).

Needs a downloaded model folder -- see scripts/download_vosk_model.py.
Accuracy is a property of the model, not this glue code: if specific
words/phrases still come out wrong after trying this for a while, the fix
is a bigger model (swap DENISSE_VOSK_MODEL_PATH to point at
vosk-model-en-us-0.22 instead of the smaller -lgraph one), not more code
here.

Capture note (found after Vosk's accuracy came in noticeably worse than
the old Google-backed setup, on the same physical microphone): PyAudio's
default input device on this machine resolves to Windows' old MME host
API, which reports a "compatible" 44.1kHz native rate and does its own
low-quality real-time resample if asked to open a stream at a different
rate. That was always happening -- Google's cloud model was just robust
enough to shrug off the resample artifacts; Vosk's offline model isn't.
So this opens the mic via WASAPI instead (Windows' modern audio API,
reports the *actual* shared-mode rate -- 48kHz on this machine, not
44.1kHz) at its true native rate, and downsamples to the 16kHz Vosk wants
itself, in software, via audioop.ratecv (which keeps filter state across
chunks -- a fresh per-chunk conversion would click at chunk boundaries).
"""

import array
import audioop
import collections
import json
import os
import queue
import threading
import time

import pyaudio
import vosk

vosk.SetLogLevel(-1)  # Vosk's C++ layer logs decoder internals to stderr by default

SAMPLE_RATE = 16000
# ~0.125s per read -- short enough that partial-result updates feel live,
# long enough not to risk buffer underruns on a background thread.
CHUNK_FRAMES = 2000
# Mirrors the old recognize_google(show_all=True) check (see wake_word.py's
# _listen_for_wake_word history): a mangled "Denisse" is more likely to
# show up in a secondary alternative than Vosk's single top guess.
MAX_ALTERNATIVES = 5

# Noise gate -- found live: Vosk's decoder doesn't just hallucinate a
# filler word at the start of real speech (see _GREETING_SUFFIX_RE in
# wake_word.py), it hallucinates short words (usually "the") from plain
# ambient room noise with *no one talking at all*. A quiet, unspoken-to
# room shouldn't produce transcripts. The floor is tracked continuously
# (see StreamingSTT._capture_loop's rolling-percentile logic), not
# calibrated once -- a one-shot calibration was tried first and found
# fragile: if that particular second happened to be unusually quiet, the
# gate stayed miscalibrated for the rest of the session while the room's
# actual steady ambient noise kept clearing it. Any chunk whose RMS
# doesn't clear floor * NOISE_GATE_MULTIPLIER gets replaced with real
# silence before reaching Vosk -- it never sees the noise, so it can't
# transcribe it.
# 5x (not the original 2.5x) -- measured live: a quiet room's ambient
# floor can still carry brief transient spikes (a click, HVAC, electrical
# noise) many times its steady median level, and 2.5x wasn't enough
# margin against those. Real speech measured 700-1900+ RMS, so even 5x a
# typical floor (~30-150) leaves a wide safety margin before this could
# ever risk clipping actual speech.
NOISE_GATE_MULTIPLIER = float(os.environ.get("DENISSE_VOSK_NOISE_GATE_MULTIPLIER", "5.0"))
NOISE_GATE_MIN_FLOOR = 30  # floor clamp for an unrealistically silent room

# Dedicated wake-word spotter -- found live: "Denisse" isn't an English
# dictionary word, so the general (open-vocabulary) recognizer above
# doesn't just mishear it occasionally, it sometimes never produces
# anything close -- observed splitting it into two unrelated real words
# ("the nice") instead of the near-miss "denise" it gets right other
# times. A second Vosk recognizer runs on the same audio, constrained via
# a grammar to choose only between this small set of phrases or "[unk]"
# (Vosk's catch-all for "none of the above") -- not competing against the
# model's whole ~100k-word vocabulary makes it dramatically more reliable
# at this one narrow job, the standard Vosk technique for keyword
# spotting. It only ever answers "was her name said" -- the general
# recognizer's own transcript is still what supplies the actual command
# text (see wake_word.py's use of the "wake_detected" event below).
WAKE_GRAMMAR = json.dumps([
    "denisse", "denise", "hey denisse", "hey denise", "hi denisse", "hi denise", "[unk]",
])

_ROOT = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_MODEL_PATH = os.path.join(_ROOT, "models", "vosk-model-en-us-0.22-lgraph")
MODEL_PATH = os.environ.get("DENISSE_VOSK_MODEL_PATH", _DEFAULT_MODEL_PATH)


class ModelUnavailable(RuntimeError):
    pass


_model = None
_model_lock = threading.Lock()


def _get_model() -> "vosk.Model":
    global _model
    with _model_lock:
        if _model is None:
            if not os.path.isdir(MODEL_PATH):
                raise ModelUnavailable(
                    f"Vosk model not found at {MODEL_PATH!r} -- run "
                    f"'python scripts/download_vosk_model.py' once to fetch it."
                )
            _model = vosk.Model(MODEL_PATH)
        return _model


def _pick_input_device(pa: "pyaudio.PyAudio") -> tuple[int, int, int]:
    """Returns (device_index, native_sample_rate, native_channels) for the
    mic to capture from -- prefers WASAPI's default input device (see the
    module docstring for why); falls back to whatever PyAudio's own default
    input device is if WASAPI isn't available (e.g. running under Wine, or
    a stripped-down Windows install). Always the device's *native* channel
    count (see StreamingSTT -- WASAPI asked to downmix stereo to mono
    itself was measured, directly, to silently corrupt the audio content
    on this machine's array mic: same energy level, unintelligible
    transcript, vs. a clean one capturing true stereo and downmixing in
    software instead)."""
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        index = wasapi.get("defaultInputDevice", -1)
        if index is not None and index >= 0:
            dev = pa.get_device_info_by_index(index)
            return index, int(dev["defaultSampleRate"]), max(1, int(dev["maxInputChannels"]))
    except Exception:
        pass
    dev = pa.get_default_input_device_info()
    return dev["index"], int(dev["defaultSampleRate"]), max(1, int(dev["maxInputChannels"]))


def _first_channel(raw: bytes, channels: int) -> bytes:
    """Deinterleave a >2-channel 16-bit PCM buffer, keeping just channel 0
    (audioop.tomono only handles stereo). Not expected to run in practice
    for a microphone, but cheap and correct if it ever does."""
    a = array.array("h")
    a.frombytes(raw)
    return a[0::channels].tobytes()


class StreamingSTT:
    """Continuously listens on the default microphone and pushes
    (kind, text, alternatives) tuples onto `.events`, a queue.Queue:
    ("partial", text, None) as words firm up while you're still talking,
    ("final", text, alternatives) once Vosk decides the utterance ended
    (`alternatives` is the ranked list of guesses, `text` its top one),
    ("wake_detected", phrase, None) whenever the dedicated grammar-
    constrained spotter (see WAKE_GRAMMAR) independently confirms her name
    was said in the current utterance -- a much more reliable signal than
    trying to spot her name in the general recognizer's own guess, but
    without her name's actual wording, so pair it with the "final" event
    for the same utterance to get real command text (see wake_word.py).
    Runs capture on its own daemon thread -- call start() once, stop() when
    done to release the microphone."""

    def __init__(self):
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._pa = None
        self._stream = None

    def start(self) -> None:
        model = _get_model()  # raises ModelUnavailable before touching any audio device
        recognizer = vosk.KaldiRecognizer(model, SAMPLE_RATE)
        recognizer.SetMaxAlternatives(MAX_ALTERNATIVES)
        spotter = vosk.KaldiRecognizer(model, SAMPLE_RATE, WAKE_GRAMMAR)

        self._pa = pyaudio.PyAudio()
        device_index, native_rate, native_channels = _pick_input_device(self._pa)
        native_chunk = max(1, int(CHUNK_FRAMES * native_rate / SAMPLE_RATE))
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=native_channels,
            rate=native_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=native_chunk,
        )

        # Capture + preprocessing (downmix/resample/noise-gate) on one
        # thread, feeding two independent decode threads -- one per
        # recognizer, each on its own queue. Found live: running both
        # recognizers' AcceptWaveform() back to back on a single thread
        # made per-chunk processing (measured ~100ms+ for the general
        # recognizer alone) creep close enough to the 125ms real-time
        # budget (CHUNK_FRAMES / SAMPLE_RATE) that ordinary jitter (other
        # tray.py threads competing for CPU, real speech costing more to
        # decode than the synthetic audio used to first measure this)
        # pushed it over, and PyAudio silently drops audio it can't keep
        # up with (exception_on_overflow=False) -- garbled everything, not
        # just the wake word. Vosk's decode work is a C++ extension doing
        # real computation, the kind that typically releases the GIL, so
        # splitting it across threads lets it actually use more than one
        # CPU core instead of serializing on one.
        general_queue: "queue.Queue[bytes]" = queue.Queue()
        spotter_queue: "queue.Queue[bytes]" = queue.Queue()
        capture_thread = threading.Thread(
            target=self._capture_loop,
            args=(general_queue, spotter_queue, native_rate, native_channels, native_chunk),
            daemon=True,
        )
        general_thread = threading.Thread(
            target=self._general_decode_loop, args=(recognizer, general_queue), daemon=True
        )
        spotter_thread = threading.Thread(
            target=self._spotter_decode_loop, args=(spotter, spotter_queue), daemon=True
        )
        self._threads = [capture_thread, general_thread, spotter_thread]
        for t in self._threads:
            t.start()

    def _downmix_and_resample(self, raw: bytes, native_rate: int, native_channels: int, ratecv_state):
        if native_channels == 2:
            raw = audioop.tomono(raw, 2, 0.5, 0.5)
        elif native_channels > 2:
            # Uncommon for a mic -- audioop.tomono only handles stereo, so
            # just take the first channel rather than mixing wrong.
            raw = _first_channel(raw, native_channels)
        if native_rate == SAMPLE_RATE:
            return raw, ratecv_state
        return audioop.ratecv(raw, 2, 1, native_rate, SAMPLE_RATE, ratecv_state)

    def _capture_loop(self, general_queue, spotter_queue, native_rate: int, native_channels: int, native_chunk: int) -> None:
        # Adaptive noise floor -- found live: a ONE-TIME 1s calibration at
        # start() is fragile. If that particular second happened to be
        # unusually quiet (lower than the room's actual steady-state
        # noise -- a fan, hum, distant traffic), the gate stays
        # miscalibrated for the rest of the session: real ambient noise
        # keeps clearing a threshold that was set too low, and Vosk keeps
        # hallucinating words from it (observed directly: 14 straight
        # "the" transcripts from continuous ambient noise, not speech).
        # Fix: track a rolling window of recent chunk RMS values and
        # recompute the floor from its low percentile continuously, not
        # once. A low percentile (not the mean) stays robust even with
        # real speech regularly passing through the window -- speech is
        # loud and brief, so it can't drag a low percentile upward the way
        # it could drag a plain average. This self-corrects to whatever
        # the room's actual ambient level is/becomes, automatically.
        rolling_window_seconds = 12.0
        chunk_seconds = CHUNK_FRAMES / SAMPLE_RATE
        maxlen = max(8, int(rolling_window_seconds / chunk_seconds))
        recent_levels: "collections.deque[int]" = collections.deque(maxlen=maxlen)
        noise_floor = NOISE_GATE_MIN_FLOOR  # conservative until the window fills

        # Downsample state carried across reads (audioop.ratecv needs its
        # own running filter state between chunks to avoid audible clicks
        # at chunk boundaries) -- see the module docstring on why this is
        # done in software instead of asking PyAudio to open the stream at
        # 16kHz directly.
        ratecv_state = None
        while not self._stop_event.is_set():
            try:
                raw = self._stream.read(native_chunk, exception_on_overflow=False)
            except Exception:
                break  # stream closed out from under us (stop() tearing down) -- just exit
            data, ratecv_state = self._downmix_and_resample(raw, native_rate, native_channels, ratecv_state)
            level = audioop.rms(data, 2)

            gate_level = noise_floor * NOISE_GATE_MULTIPLIER
            if level < gate_level:
                # Below the current floor -- real silence instead of
                # whatever ambient hiss/hum is actually there, so Vosk has
                # nothing to hallucinate a word out of (see
                # NOISE_GATE_MULTIPLIER above).
                data = b"\x00\x00" * (len(data) // 2)

            # Every chunk feeds the rolling window unconditionally (not
            # just "quiet" ones) -- gating which samples count would let
            # an initially-too-low floor get stuck, since noise sitting
            # above a too-low gate would never be classified as quiet
            # enough to correct it. The low percentile below does that
            # filtering safely instead.
            recent_levels.append(level)
            if len(recent_levels) >= 8:
                sorted_levels = sorted(recent_levels)
                percentile_20 = sorted_levels[max(0, int(len(sorted_levels) * 0.2) - 1)]
                noise_floor = max(NOISE_GATE_MIN_FLOOR, percentile_20)

            general_queue.put(data)
            spotter_queue.put(data)
        # Wake both decode loops out of a blocking get() so they notice
        # _stop_event promptly instead of waiting on their own timeout.
        general_queue.put(None)
        spotter_queue.put(None)

    def _general_decode_loop(self, recognizer, chunks: "queue.Queue[bytes]") -> None:
        last_partial = ""
        while not self._stop_event.is_set():
            try:
                data = chunks.get(timeout=0.5)
            except queue.Empty:
                continue
            if data is None:
                break
            if recognizer.AcceptWaveform(data):
                result = json.loads(recognizer.Result())
                alternatives = [a.get("text", "") for a in result.get("alternatives", []) if a.get("text")]
                if not alternatives and result.get("text"):
                    alternatives = [result["text"]]
                last_partial = ""
                if alternatives:
                    self.events.put(("final", alternatives[0], alternatives))
            else:
                partial = json.loads(recognizer.PartialResult()).get("partial", "")
                if partial and partial != last_partial:
                    last_partial = partial
                    self.events.put(("partial", partial, None))

    def _spotter_decode_loop(self, spotter, chunks: "queue.Queue[bytes]") -> None:
        # Grammar-constrained -- only ever answers "was her name said" (see
        # WAKE_GRAMMAR), so only its finalized result matters, not partials.
        while not self._stop_event.is_set():
            try:
                data = chunks.get(timeout=0.5)
            except queue.Empty:
                continue
            if data is None:
                break
            if spotter.AcceptWaveform(data):
                spotted = json.loads(spotter.Result()).get("text", "").strip()
                if spotted and spotted != "[unk]":
                    self.events.put(("wake_detected", spotted, None))

    def stop(self) -> None:
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=2)
        self._threads = []
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass


def transcribe_once(timeout: float = 8.0, phrase_time_limit: float = 15.0) -> str:
    """One-shot blocking transcription for push-to-talk callers
    (voice_mode.py): waits up to `timeout` seconds for speech to start,
    then up to `phrase_time_limit` more for it to finish, returning the
    transcript ('' if nothing understandable was heard). Hands-free
    listening (wake_word.py) uses StreamingSTT directly instead, so it can
    react to live partial results while composing/speaking a reply."""
    stt = StreamingSTT()
    try:
        stt.start()
    except ModelUnavailable:
        raise

    try:
        start_deadline = time.time() + timeout
        phrase_deadline = None
        while True:
            deadline = phrase_deadline if phrase_deadline is not None else start_deadline
            remaining = deadline - time.time()
            if remaining <= 0:
                return ""
            try:
                kind, text, _alts = stt.events.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if kind == "partial":
                if text and phrase_deadline is None:
                    phrase_deadline = time.time() + phrase_time_limit
                continue
            if kind == "wake_detected":
                continue  # not relevant to push-to-talk -- only StreamingSTT's "final" is
            return text
    finally:
        stt.stop()
