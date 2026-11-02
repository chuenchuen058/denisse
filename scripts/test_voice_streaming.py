"""Streaming voice: sentence splitting, chunk playback order, barge-in,
endpointing and the latency log -- with fake STT/TTS/model streams.

    .venv\\Scripts\\python.exe -m unittest discover -s scripts -p "test_voice_streaming.py" -v
No audio device, GPU or model is touched.
"""
import os
import random
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import voice_pipeline as vp  # noqa: E402


class FakeSink:
    """Records what would have reached the speakers, in order."""

    def __init__(self):
        self.events = []
        self.samples = []
        self.lock = threading.Lock()
        self.first_write = threading.Event()

    def open(self, rate):
        self.events.append(("open", rate))

    def write(self, samples):
        with self.lock:
            self.samples.extend(samples)
        self.events.append(("write", len(samples)))
        self.first_write.set()

    def finish(self):
        self.events.append(("finish",))

    def abort(self):
        self.events.append(("abort",))


RATE = 1000  # tiny fake rate: 1 sample = 1 ms


def tagged_tts(delays=None, fail=()):
    """Each sentence becomes samples all equal to its index+1, so the order
    of what was played can be read straight back from the sink."""
    names = {}
    calls = []

    def synth(sentence):
        calls.append(sentence)
        idx = names.setdefault(sentence, len(names) + 1)
        if delays:
            time.sleep(delays(idx))
        if sentence in fail:
            return None
        return [float(idx)] * 120, RATE
    return synth, names, calls


def played_order(sink):
    order = []
    for x in sink.samples:
        if x and (not order or order[-1] != x):
            order.append(x)
    return order


def stream_of(text, size):
    """A fake model stream: the reply in `size`-character chunks."""
    return [text[i:i + size] for i in range(0, len(text), size)]


class Splitter(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(vp.split_sentences("Hi there. How are you? Great!"),
                         ["Hi there.", "How are you?", "Great!"])

    def test_chunking_does_not_change_result(self):
        text = ("Sure, Dr. Patel said it's 3.5 hours. The meeting is at 2 p.m. on Friday! "
                "Want me to add it?\nAlso: e.g. the slides... Done")
        whole = vp.split_sentences(text)
        for size in (1, 2, 3, 7, 50):
            s = vp.SentenceSplitter()
            got = []
            for c in stream_of(text, size):
                got += s.feed(c)
            got += s.flush()
            self.assertEqual(got, whole, f"chunk size {size}")

    def test_abbreviations_decimals_initials_dont_split(self):
        self.assertEqual(vp.split_sentences("Ask Dr. Smith about 3.14 today. J. R. Tolkien wrote it."),
                         ["Ask Dr. Smith about 3.14 today.", "J. R. Tolkien wrote it."])
        self.assertEqual(vp.split_sentences("Bring snacks, e.g. crisps. Thanks."),
                         ["Bring snacks, e.g. crisps.", "Thanks."])

    def test_numbered_list_and_newlines(self):
        self.assertEqual(vp.split_sentences("Your tasks:\n1. Buy milk\n2. Email tutor"),
                         ["Your tasks:", "1. Buy milk", "2. Email tutor"])
        self.assertEqual(vp.split_sentences("It was founded in 2026. Next one."),
                         ["It was founded in 2026.", "Next one."])

    def test_trailing_stop_waits_for_more_text(self):
        s = vp.SentenceSplitter()
        self.assertEqual(s.feed("It costs 3."), [])     # might be 3.50
        self.assertEqual(s.feed("50 pounds. Ok"), ["It costs 3.50 pounds."])
        self.assertEqual(s.flush(), ["Ok"])

    def test_quotes_stay_with_their_sentence(self):
        self.assertEqual(vp.split_sentences('She said "go." Then left.'),
                         ['She said "go."', "Then left."])

    def test_long_run_on_is_capped(self):
        text = ", ".join(["word number %d" % i for i in range(60)])
        parts = vp.split_sentences(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= vp.MAX_SENTENCE_CHARS for p in parts))
        self.assertEqual(" ".join(parts).replace(" ", ""), text.replace(" ", ""))

    def test_empty(self):
        self.assertEqual(vp.split_sentences(""), [])
        self.assertEqual(vp.split_sentences("   \n "), [])


class ChunkPlayer(unittest.TestCase):
    def test_plays_every_sentence_in_order_despite_uneven_synthesis(self):
        rnd = random.Random(7)
        synth, names, _ = tagged_tts(delays=lambda i: rnd.random() * 0.02)
        sink = FakeSink()
        text = "One. Two is longer than one. Three! Four? Five."
        sp = vp.StreamingSpeaker(synth, sink, lead_in_s=0, block_s=0.03)
        self.assertTrue(sp.speak(stream_of(text, 4)))
        self.assertEqual(played_order(sink), [1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(sp.spoken, vp.split_sentences(text))
        # One stream for the whole reply: opened once, drained once, no gaps.
        self.assertEqual([e for e in sink.events if e[0] != "write"], [("open", RATE), ("finish",)])
        self.assertEqual(len(sink.samples), 5 * 120)

    def test_first_sentence_plays_before_the_rest_is_synthesized(self):
        sink = FakeSink()
        release = threading.Event()
        seen_by_second = []

        def synth(sentence):
            if sentence.startswith("Second"):
                # Proves streaming: sentence 1 is already audible while
                # sentence 2 is still being synthesized.
                seen_by_second.append(sink.first_write.wait(2))
                release.wait(2)
            return [0.5] * 50, RATE

        def unblock():
            sink.first_write.wait(2)
            release.set()
        threading.Thread(target=unblock, daemon=True).start()
        vp.StreamingSpeaker(synth, sink, lead_in_s=0).speak(["First one. ", "Second one."])
        self.assertEqual(seen_by_second, [True])

    def test_first_sentence_starts_while_model_still_generating(self):
        sink = FakeSink()
        synth, _, _ = tagged_tts()
        model_waited = []

        def model_stream():
            yield "Hello there. "
            model_waited.append(sink.first_write.wait(2))  # rest still "generating"
            yield "And the rest."
        vp.StreamingSpeaker(synth, sink, lead_in_s=0).speak(model_stream())
        self.assertEqual(model_waited, [True])
        self.assertEqual(played_order(sink), [1.0, 2.0])

    def test_lead_in_silence_comes_first(self):
        synth, _, _ = tagged_tts()
        sink = FakeSink()
        vp.StreamingSpeaker(synth, sink, lead_in_s=0.03).speak(["Hi."])
        self.assertEqual(sink.samples[:30], [0.0] * 30)
        self.assertEqual(sink.samples[30], 1.0)

    def test_failed_sentence_is_skipped_rest_still_plays(self):
        synth, _, _ = tagged_tts(fail={"Two."})
        sink = FakeSink()
        sp = vp.StreamingSpeaker(synth, sink, lead_in_s=0)
        self.assertTrue(sp.speak(["One. Two. Three."]))
        self.assertEqual(sp.spoken, ["One.", "Three."])

    def test_nothing_synthesized_returns_false_for_fallback(self):
        sink = FakeSink()
        sp = vp.StreamingSpeaker(lambda s: None, sink, lead_in_s=0)
        self.assertFalse(sp.speak(["One. Two."]))
        self.assertEqual(sink.events, [])

    def test_trace_stages_in_order(self):
        clock = iter(range(100, 1000))
        trace = vp.LatencyTrace(clock=lambda: float(next(clock)))
        trace.mark("speech_last_voice")
        trace.mark("speech_end")
        trace.mark("stt_done")
        synth, _, _ = tagged_tts()
        vp.StreamingSpeaker(synth, FakeSink(), trace=trace, lead_in_s=0).speak(["A. B."])
        d = trace.deltas()
        self.assertEqual(list(d), list(vp.STAGES))
        self.assertEqual(sorted(d.values()), list(d.values()))


class BargeIn(unittest.TestCase):
    def test_stop_midway_aborts_and_stops_synthesis(self):
        synth, _, calls = tagged_tts()
        sink = FakeSink()
        writes = []
        orig = sink.write

        def counting_write(samples):
            writes.append(1)
            orig(samples)
        sink.write = counting_write
        # He starts talking during the second block of the first sentence.
        sp = vp.StreamingSpeaker(synth, sink, should_stop=lambda: len(writes) >= 2,
                                 lead_in_s=0, block_s=0.03)
        self.assertTrue(sp.speak(["One. Two. Three. Four. Five. Six."]))
        self.assertTrue(sp.interrupted)
        self.assertEqual(sink.events[-1], ("abort",))
        self.assertNotIn(("finish",), sink.events)
        self.assertEqual(len(writes), 2)        # nothing more reached the speakers
        self.assertEqual(sp.spoken, [])
        self.assertLessEqual(len(calls), 1 + 2 + 1)  # synthesis stayed <= `ahead` in front

    def test_stop_before_anything_plays(self):
        synth, _, _ = tagged_tts()
        sink = FakeSink()
        sp = vp.StreamingSpeaker(synth, sink, should_stop=lambda: True)
        self.assertTrue(sp.speak(["One. Two."]))  # stopped, so no fallback voice either
        self.assertEqual(sink.events, [])

    def test_on_start_fires_once_before_first_audio(self):
        synth, _, _ = tagged_tts()
        sink = FakeSink()
        sp = vp.StreamingSpeaker(synth, sink, lead_in_s=0,
                                 on_start=lambda: sink.events.append(("start",)))
        sp.speak(["One. Two. Three."])
        starts = [i for i, e in enumerate(sink.events) if e == ("start",)]
        first_write = next(i for i, e in enumerate(sink.events) if e[0] == "write")
        self.assertEqual(len(starts), 1)
        self.assertLess(starts[0], first_write)

    def test_on_start_not_fired_when_nothing_synthesized_or_stopped_first(self):
        fired = []
        vp.StreamingSpeaker(lambda s: None, FakeSink(), on_start=lambda: fired.append(1)).speak(["One."])
        synth, _, _ = tagged_tts()
        vp.StreamingSpeaker(synth, FakeSink(), should_stop=lambda: True,
                            on_start=lambda: fired.append(1)).speak(["One."])
        self.assertEqual(fired, [])

    def test_stop_during_on_start_writes_nothing(self):
        """A stop pressed while the bubble reveal runs still wins."""
        stopped = threading.Event()
        synth, _, _ = tagged_tts()
        sink = FakeSink()
        sp = vp.StreamingSpeaker(synth, sink, should_stop=stopped.is_set, on_start=stopped.set)
        self.assertTrue(sp.speak(["One. Two."]))
        self.assertTrue(sp.interrupted)
        self.assertEqual([e for e in sink.events if e[0] == "write"], [])
        self.assertIn(("abort",), sink.events)

    def test_detector_needs_sustained_speech_above_her_echo(self):
        det = vp.BargeInDetector(chunk_s=0.064, min_loud_s=0.3, factor=4, min_rms=300, calibrate_s=0.3)
        for _ in range(det.n_cal):
            self.assertFalse(det.push(500))          # her voice leaking: floor 500
        self.assertFalse(det.push(1500))             # louder echo, under 4x
        for _ in range(det.need - 1):
            self.assertFalse(det.push(5000))
        self.assertFalse(det.push(100))              # a blip resets the run
        for _ in range(det.need - 1):
            self.assertFalse(det.push(5000))
        self.assertTrue(det.push(5000))

    def test_mic_barge_in_wired_to_speaker(self):
        """The event MicBargeIn sets is what voice_mode polls."""
        ev = threading.Event()
        synth, _, _ = tagged_tts(delays=lambda i: 0.01)
        sink = FakeSink()
        threading.Timer(0.0, ev.set).start()
        time.sleep(0.01)
        sp = vp.StreamingSpeaker(synth, sink, should_stop=ev.is_set)
        sp.speak(["One. Two."])
        self.assertTrue(sp.interrupted)


class VoiceModeWiring(unittest.TestCase):
    """voice_mode.speak(): Kokoro streams; anything else uses the old path."""

    def setUp(self):
        import importlib
        import types
        os.environ["DENISSE_TTS"] = "kokoro"
        self.fake = types.ModuleType("tts_kokoro")
        self.fake.available = lambda: True
        self.fake.synthesize_samples = lambda s: ([0.1] * 100, RATE)
        sys.modules["tts_kokoro"] = self.fake
        import voice_output
        importlib.reload(voice_output)
        import voice_mode
        self.vm = importlib.reload(voice_mode)
        self.sink = FakeSink()
        self.vm._sink = self.sink
        self.inworld = []
        self.vm.speak_inworld = lambda text: self.inworld.append(text) or True

    def tearDown(self):
        sys.modules.pop("tts_kokoro", None)
        os.environ.pop("DENISSE_TTS", None)

    def test_kokoro_streams_and_traces(self):
        trace = vp.LatencyTrace()
        trace.mark("speech_last_voice")
        self.vm.speak(None, "One. Two.", trace)
        self.assertEqual(self.inworld, [])
        self.assertIn(("finish",), self.sink.events)
        self.assertEqual(trace.extra["sentences"], 2)
        self.assertIn("playback_start", trace.deltas())

    def test_falls_back_when_kokoro_unavailable(self):
        self.fake.available = lambda: False
        self.vm.speak(None, "One. Two.")
        self.assertEqual(self.inworld, ["One. Two."])
        self.assertEqual(self.sink.events, [])


class Endpointing(unittest.TestCase):
    def run_clip(self, rms_seq, end_silence_s, chunk_s=0.064):
        ep = vp.Endpointer(chunk_s=chunk_s, end_silence_s=end_silence_s, min_rms=300,
                           timeout=5, phrase_time_limit=15, calibrate_s=0.4)
        for i, r in enumerate(rms_seq):
            v = ep.push(r, i * chunk_s)
            if v:
                return v, i * chunk_s, ep
        return None, None, ep

    def test_ends_after_configured_silence(self):
        quiet, loud = [50] * 7, [3000] * 20
        seq = quiet + loud + quiet * 10
        for end_s in (0.9, 0.5):
            v, t, ep = self.run_clip(seq, end_s)
            self.assertEqual(v, "end")
            self.assertGreater(t - ep.last_voice, end_s)
            self.assertLess(t - ep.last_voice, end_s + 0.07)

    def test_shorter_threshold_cuts_at_a_long_pause(self):
        seq = [50] * 7 + [3000] * 10 + [50] * 10 + [3000] * 10 + [50] * 30  # 0.64 s pause
        self.assertEqual(self.run_clip(seq, 0.5)[0], "end")
        v, t, ep = self.run_clip(seq, 0.9)
        self.assertGreater(ep.last_voice, (7 + 10 + 10) * 0.064)  # heard the second half
        self.assertAlmostEqual(ep.longest_pause, 10 * 0.064, places=3)  # logged per turn

    def test_timeout_without_speech(self):
        self.assertEqual(self.run_clip([50] * 200, 0.9)[0], "timeout")

    def test_env_configures_default(self):
        self.assertEqual(vp.Endpointer(chunk_s=0.064).end_silence_s, vp.END_SILENCE_S)


class LatencyLog(unittest.TestCase):
    def test_write_read_breakdown(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "lat.jsonl")
            for i in range(10):
                t = vp.LatencyTrace()
                base = 1000.0 + i
                t.mark("speech_last_voice", base)
                t.mark("speech_end", base + 0.5)
                t.mark("stt_done", base + 0.9 + i * 0.01)
                t.write(path)
            vp.LatencyTrace().write(path)  # empty trace: nothing written
            rows = vp.read_log(path)
            self.assertEqual(len(rows), 10)
            b = vp.breakdown(rows)
            self.assertEqual(b["speech_end"], {"n": 10, "p50": 0.5, "p95": 0.5})
            self.assertAlmostEqual(b["stt_done"]["p50"], 0.945, places=3)
            self.assertNotIn("playback_start", b)

    def test_breakdown_by_source_and_typed_origin(self):
        rows = []
        for src, first in (("voice_mode", 2.0), ("tray_chat", 1.0), (None, 2.0)):
            t = vp.LatencyTrace()
            t.mark("input_end" if src == "tray_chat" else "speech_last_voice", 100.0)
            t.mark("first_token", 100.0 + first)
            if src:
                t.extra["source"] = src
            rows.append(t.to_record())
        self.assertEqual(vp.breakdown(rows, "tray_chat")["first_token"]["n"], 1)
        self.assertEqual(vp.breakdown(rows, "tray_chat")["first_token"]["p50"], 1.0)
        self.assertEqual(vp.breakdown(rows, "voice_mode")["first_token"]["n"], 2)  # untagged = voice_mode
        self.assertEqual(vp.breakdown(rows)["first_token"]["n"], 3)

    def test_percentile(self):
        self.assertIsNone(vp.percentile([], 50))
        self.assertEqual(vp.percentile([3, 1, 2], 50), 2)
        self.assertAlmostEqual(vp.percentile(list(range(1, 101)), 95), 95.05)

    def test_write_never_raises(self):
        t = vp.LatencyTrace()
        t.mark("stt_done")
        t.write(os.path.join(ROOT, "voice_pipeline.py", "cannot", "be", "a", "dir.jsonl"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
