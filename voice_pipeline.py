"""
Denisse -- the latency-critical part of a spoken turn, kept free of audio
and model imports so it can be unit-tested with fakes
(scripts/test_voice_streaming.py).

What lives here:
- LatencyTrace: per-turn timestamps for each stage, appended as one JSON line
  to logs/voice_latency.jsonl (local only, git-ignored, never the vault).
  `scripts/eval_local_voice.py --breakdown` prints p50/p95 from it.
- SentenceSplitter: turns a stream of text chunks into complete sentences, so
  speech can start on the first one while the rest is still being made.
- StreamingSpeaker: synthesizes sentence N+1 while sentence N plays, writes
  them back to back into ONE open output stream (no gaps, no per-file open),
  and stops cleanly when told to (stop button or barge-in).
- Endpointer: the "has he stopped talking?" decision from stt_whisper's mic
  loop, pulled out so the eval can replay fixed audio through it offline.
- BargeInDetector: the same energy gate, used while Denisse is speaking.

Measured numbers and the reasoning behind the defaults: docs/voice_latency.md.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from typing import Callable, Iterable

_ROOT = os.path.dirname(os.path.abspath(__file__))
LATENCY_LOG = os.environ.get("DENISSE_VOICE_LATENCY_LOG",
                             os.path.join(_ROOT, "logs", "voice_latency.jsonl"))

# The order a turn passes through. "speech_last_voice" is the last chunk that
# was loud -- roughly when he actually stopped talking -- and is what every
# other stage is measured from, so the endpointing wait counts too.
# "first_token" is the first text the speaker was handed; brain.respond() is
# not streamed (its reply guards rewrite the finished text), so for now that
# is the moment the whole reply came back. See docs/voice_latency.md.
STAGES = ("speech_last_voice", "speech_end", "stt_done", "first_token",
          "first_sentence", "tts_first_chunk", "playback_start")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# -- per-turn timing ----------------------------------------------------------

class LatencyTrace:
    """First-wins timestamps for one turn. Thread-safe: the synthesis thread
    and the playback thread both mark stages."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self.marks: dict[str, float] = {}
        self.extra: dict = {}

    def mark(self, stage: str, t: float | None = None) -> None:
        with self._lock:
            if stage not in self.marks:
                self.marks[stage] = self._clock() if t is None else t

    def origin(self) -> float | None:
        return self.marks.get("speech_last_voice", self.marks.get("speech_end"))

    def deltas(self) -> dict[str, float]:
        """Seconds from end of speech to each stage that was reached."""
        t0 = self.origin()
        if t0 is None:
            return {}
        return {s: round(self.marks[s] - t0, 3) for s in STAGES if s in self.marks}

    def to_record(self) -> dict:
        return {"at": round(self.marks.get("speech_end", self._clock()), 3),
                "marks": {k: round(v, 3) for k, v in self.marks.items()},
                "deltas": self.deltas(), **self.extra}

    def write(self, path: str | None = None) -> None:
        """Append this turn to the local log. Never raises: timing must not
        be the thing that breaks a reply."""
        if not self.marks:
            return
        path = path or LATENCY_LOG
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self.to_record()) + "\n")
        except Exception:
            pass


def read_log(path: str | None = None) -> list[dict]:
    rows = []
    try:
        with open(path or LATENCY_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return rows


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile (p in 0..100); None for no data."""
    xs = sorted(values)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def breakdown(rows: list[dict]) -> dict[str, dict]:
    """{stage: {"n", "p50", "p95"}} of seconds-since-end-of-speech."""
    out = {}
    for stage in STAGES:
        vals = [r["deltas"][stage] for r in rows if stage in r.get("deltas", {})]
        if vals:
            out[stage] = {"n": len(vals), "p50": round(percentile(vals, 50), 3),
                          "p95": round(percentile(vals, 95), 3)}
    return out


# -- sentence splitting -------------------------------------------------------

# Words that end in a full stop without ending the sentence.
_ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "eg", "ie",
    "e.g", "i.e", "a.m", "p.m", "am", "pm", "no", "fig", "approx", "dept", "est",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "mon", "tue", "wed", "thu", "fri", "sat", "sun", "u.s", "u.k",
}
_END = re.compile(r"[.!?…]+[\"')\]]*")
MAX_SENTENCE_CHARS = 300


class SentenceSplitter:
    """feed() text as it arrives; get back the sentences that are now known
    to be complete. flush() returns whatever is left at the end.

    A sentence ends at . ! ? or an ellipsis that is followed by whitespace,
    or at a line break -- so "3.5", "e.g. this", "Dr. Smith" and the
    "1." of a numbered list don't split, and a full stop at the very end
    of the text so far waits for the next chunk before deciding."""

    def __init__(self, max_chars: int = MAX_SENTENCE_CHARS):
        self.buf = ""
        self.max_chars = max_chars

    def feed(self, text: str) -> list[str]:
        self.buf += text or ""
        out = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            piece, self.buf = self.buf[:cut], self.buf[cut:]
            piece = piece.strip()
            if piece:
                out.append(piece)
        return out

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        return self._cap(rest) if rest else []

    def _cap(self, text: str) -> list[str]:
        """Split an over-long run with no sentence end, at the last comma or
        space before the cap, so one run-on can't hold up the first audio."""
        out = []
        while len(text) > self.max_chars:
            window = text[:self.max_chars]
            at = max(window.rfind(", "), window.rfind("; "))
            at = at + 1 if at > self.max_chars // 3 else window.rfind(" ")
            if at <= 0:
                at = self.max_chars
            out.append(text[:at].strip())
            text = text[at:].strip()
        if text:
            out.append(text)
        return out

    def _find_cut(self) -> int | None:
        buf = self.buf
        nl = buf.find("\n")
        for m in _END.finditer(buf):
            if nl != -1 and nl < m.start():
                break
            end = m.end()
            if end >= len(buf):
                return None  # can't tell yet: "3." might become "3.5"
            if not buf[end].isspace():
                continue  # "3.5", "e.g.x", "file.txt"
            if m.group().startswith(".") and self._not_a_stop(buf[:m.start()]):
                continue
            return end
        if nl != -1:
            return nl + 1
        if len(buf.strip()) > self.max_chars:
            # A long run with no end yet: hand out the capped head, keep the tail.
            head = self._cap(buf.strip())[0]
            return buf.find(head) + len(head)
        return None

    @staticmethod
    def _not_a_stop(before: str) -> bool:
        word = re.search(r"([A-Za-z.]+|\d+)$", before)
        if not word:
            return False
        w = word.group(1)
        if w.isdigit():
            # "1." starting a line or the text is a list number; "in 2026." ends.
            line = before[:len(before) - len(w)].rstrip(" \t")
            return line == "" or line.endswith("\n")
        lw = w.lower().strip(".")
        if lw in _ABBREV:
            return True
        return len(w) == 1 and w.isupper()  # an initial: "J. Smith"


def split_sentences(text: str) -> list[str]:
    s = SentenceSplitter()
    return s.feed(text) + s.flush()


# -- endpointing (when has he stopped talking?) -------------------------------

# 0.6, down from 0.9: same WER on the eval set at 0.4-0.9, 0.3 cuts words
# off; the longest synthetic pause was 0.38 s and real pauses run longer,
# so 0.6 keeps a margin. docs/voice_latency.md.
END_SILENCE_S = _env_float("DENISSE_END_SILENCE_S", 0.6)
MIN_SPEECH_RMS = _env_float("DENISSE_MIN_SPEECH_RMS", 300)
CALIBRATE_S = 0.4


class Endpointer:
    """The energy gate from stt_whisper's mic loop, as a pure function of
    (chunk loudness, time). push() returns None to keep listening, or one of
    "end" (speech then `end_silence_s` of quiet), "limit" (phrase too long),
    "timeout" (no speech at all)."""

    def __init__(self, chunk_s: float, end_silence_s: float | None = None,
                 min_rms: float | None = None, timeout: float = 8.0,
                 phrase_time_limit: float = 15.0, calibrate_s: float = CALIBRATE_S):
        self.chunk_s = chunk_s
        self.end_silence_s = END_SILENCE_S if end_silence_s is None else end_silence_s
        self.min_rms = MIN_SPEECH_RMS if min_rms is None else min_rms
        self.timeout = timeout
        self.phrase_time_limit = phrase_time_limit
        self.n_cal = max(1, int(calibrate_s / chunk_s))
        self.noise: list[float] = []
        self.started_at = None
        self.last_voice = None
        self.t_start = None
        self.longest_pause = 0.0

    @property
    def gate(self) -> float:
        floor = sum(self.noise) / len(self.noise) if self.noise else 0.0
        return max(self.min_rms, 3 * floor)

    def calibrating(self) -> bool:
        return len(self.noise) < self.n_cal

    def push(self, rms: float, now: float) -> str | None:
        if self.t_start is None:
            self.t_start = now
        if self.calibrating():
            self.noise.append(rms)
            return None
        loud = rms > self.gate
        if self.started_at is None:
            if loud:
                self.started_at = self.last_voice = now
            elif now - self.t_start > self.timeout:
                return "timeout"
            return None
        if loud:
            # Longest quiet stretch *inside* the utterance: how close this
            # turn came to being cut off. Logged per turn, so the threshold
            # can be checked against his real pauses, not synthetic ones.
            self.longest_pause = max(self.longest_pause, now - self.last_voice - self.chunk_s)
            self.last_voice = now
        if now - self.last_voice > self.end_silence_s:
            return "end"
        if now - self.started_at > self.phrase_time_limit:
            return "limit"
        return None


# -- barge-in -----------------------------------------------------------------

BARGE_IN = os.environ.get("DENISSE_BARGE_IN", "0").lower() in ("1", "true", "yes", "on")
BARGE_IN_MIN_S = _env_float("DENISSE_BARGE_IN_MIN_S", 0.3)
BARGE_IN_FACTOR = _env_float("DENISSE_BARGE_IN_FACTOR", 4.0)


class BargeInDetector:
    """Is he talking over her? Needs `min_loud_s` of continuous loudness
    above `factor` x the level measured while she was already speaking --
    so her own voice leaking from the speakers into the mic sets the floor
    instead of tripping it. Off by default (DENISSE_BARGE_IN=1 to enable):
    this is an energy gate with no echo cancellation, so on open speakers a
    loud reply can still interrupt itself. Fine on headphones."""

    def __init__(self, chunk_s: float, min_loud_s: float = BARGE_IN_MIN_S,
                 factor: float = BARGE_IN_FACTOR, min_rms: float | None = None,
                 calibrate_s: float = 0.3):
        self.chunk_s = chunk_s
        self.need = max(1, int(round(min_loud_s / chunk_s)))
        self.factor = factor
        self.min_rms = MIN_SPEECH_RMS if min_rms is None else min_rms
        self.n_cal = max(1, int(calibrate_s / chunk_s))
        self.floor: list[float] = []
        self.run = 0

    def push(self, rms: float) -> bool:
        if len(self.floor) < self.n_cal:
            self.floor.append(rms)
            return False
        gate = max(self.min_rms, self.factor * (sum(self.floor) / len(self.floor)))
        self.run = self.run + 1 if rms > gate else 0
        return self.run >= self.need


class MicBargeIn:
    """Listens on the mic in a thread while she speaks and sets `event` when
    BargeInDetector fires. Any mic trouble just means no barge-in."""

    def __init__(self, event: threading.Event):
        self.event = event
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "MicBargeIn":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            import audioop
            import pyaudio
            import stt_vosk
            pa = pyaudio.PyAudio()
            try:
                index, rate, channels = stt_vosk._pick_input_device(pa)
                chunk = max(1, int(1024 * rate / 16000))
                stream = pa.open(format=pyaudio.paInt16, channels=channels, rate=rate, input=True,
                                 input_device_index=index, frames_per_buffer=chunk)
                det = BargeInDetector(chunk_s=chunk / rate)
                try:
                    while not self._stop.is_set():
                        raw = stream.read(chunk, exception_on_overflow=False)
                        if det.push(audioop.rms(stt_vosk._first_channel(raw, channels), 2)):
                            self.event.set()
                            return
                finally:
                    stream.stop_stream()
                    stream.close()
            finally:
                pa.terminate()
        except Exception:
            pass


# -- streaming speech ---------------------------------------------------------

LEAD_IN_S = _env_float("DENISSE_TTS_LEAD_IN_S", 0.03)
BLOCK_S = 0.05


class PyAudioSink:
    """One output stream for a whole reply: sentences are written back to
    back, so there's no per-file open and no gap between them. Must be used
    from a single thread."""

    def __init__(self):
        self._pa = None
        self._stream = None
        self.rate = None

    def open(self, rate: int) -> None:
        import pyaudio
        if self._stream is not None and rate == self.rate:
            return
        self.finish()
        self._pa = self._pa or pyaudio.PyAudio()
        self._stream = self._pa.open(format=pyaudio.paInt16, channels=1, rate=int(rate), output=True)
        self.rate = rate

    def write(self, samples) -> None:
        import numpy as np
        pcm = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
        self._stream.write((pcm * 32767).astype(np.int16).tobytes())

    def finish(self) -> None:
        """Let what's buffered play out (stop_stream drains), then close."""
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def abort(self) -> None:
        """Stop now: closing an active stream discards its pending buffers."""
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def close(self) -> None:
        self.finish()
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None


_DONE = object()


class StreamingSpeaker:
    """Speak a reply that arrives as text chunks.

    `synthesize(sentence) -> (samples, rate) | None` (float32 mono).
    `sink` has open(rate) / write(samples) / finish() / abort().
    `should_stop()` is polled between ~50 ms blocks; when it turns true,
    playback stops at once and no further sentence is synthesized.

    A synthesis thread runs at most `ahead` sentences in front of playback,
    so a stop never leaves the GPU busy on text nobody will hear."""

    def __init__(self, synthesize, sink, trace: LatencyTrace | None = None,
                 should_stop: Callable[[], bool] | None = None, ahead: int = 2,
                 lead_in_s: float = LEAD_IN_S, block_s: float = BLOCK_S):
        self.synthesize = synthesize
        self.sink = sink
        self.trace = trace or LatencyTrace()
        self.should_stop = should_stop or (lambda: False)
        self.ahead = max(1, ahead)
        self.lead_in_s = lead_in_s
        self.block_s = block_s
        self.spoken: list[str] = []
        self.interrupted = False
        self.errors: list[str] = []

    def _produce(self, chunks: Iterable[str], q: "queue.Queue", halt: threading.Event) -> None:
        splitter = SentenceSplitter()

        def emit(sentences) -> bool:
            for s in sentences:
                if halt.is_set():
                    return False
                self.trace.mark("first_sentence")
                try:
                    audio = self.synthesize(s)
                except Exception as e:  # one bad sentence shouldn't end the reply
                    self.errors.append(f"{s[:40]}: {e}")
                    audio = None
                if audio is None:
                    continue
                self.trace.mark("tts_first_chunk")
                while not halt.is_set():
                    try:
                        q.put((s, audio), timeout=0.05)
                        break
                    except queue.Full:
                        pass
            return not halt.is_set()

        try:
            for chunk in chunks:
                if halt.is_set():
                    return
                if chunk:
                    self.trace.mark("first_token")
                if not emit(splitter.feed(chunk)):
                    return
            emit(splitter.flush())
        except Exception as e:
            self.errors.append(str(e))
        finally:
            while True:
                try:
                    q.put(_DONE, timeout=0.05)
                    return
                except queue.Full:
                    if halt.is_set():
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            pass

    def speak(self, chunks: Iterable[str]) -> bool:
        """Blocks until the reply has played (or was stopped). Returns False
        only if nothing at all could be synthesized, so the caller can fall
        back to another voice."""
        q: queue.Queue = queue.Queue(maxsize=self.ahead)
        halt = threading.Event()
        producer = threading.Thread(target=self._produce, args=(chunks, q, halt), daemon=True)
        producer.start()
        opened = False
        open_rate = None
        try:
            while True:
                if self.should_stop():
                    self.interrupted = True
                    break
                try:
                    item = q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is _DONE:
                    break
                sentence, (samples, rate) = item
                if not opened:
                    self.sink.open(rate)
                    opened = True
                    if self.lead_in_s > 0:
                        # A few ms of silence so the device's start-up can't
                        # eat the first phoneme.
                        self.sink.write([0.0] * int(rate * self.lead_in_s))
                elif rate != open_rate:
                    self.sink.open(rate)  # a voice at another rate: reopen
                open_rate = rate
                step = max(1, int(rate * self.block_s))
                for i in range(0, len(samples), step):
                    if self.should_stop():
                        self.interrupted = True
                        break
                    self.sink.write(samples[i:i + step])
                    self.trace.mark("playback_start")
                if self.interrupted:
                    break
                self.spoken.append(sentence)
        finally:
            halt.set()
            if opened:
                if self.interrupted:
                    self.sink.abort()
                else:
                    self.sink.finish()
            producer.join(timeout=5)
        return opened or self.interrupted
