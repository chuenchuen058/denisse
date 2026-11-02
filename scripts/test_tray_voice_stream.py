"""The tray chat and the wake word speak through the streaming speaker.

    .venv\\Scripts\\python.exe -m unittest discover -s scripts -p "test_tray_voice_stream.py" -v

With local Kokoro (DENISSE_TTS=kokoro) both paths hand the reply to
voice_output.speak_streaming() -> voice_pipeline.StreamingSpeaker; with
DENISSE_TTS_STREAM=0, another engine, or Kokoro missing they keep the old
synthesize-whole-reply + MCI path. Fake TTS and a fake output sink: no
audio device, model, Tk window or real latency log is touched.
"""
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import speech_control  # noqa: E402
import tray  # noqa: E402
import voice_output  # noqa: E402
import voice_pipeline as vp  # noqa: E402
import wake_word  # noqa: E402

RATE = 1000  # 1 sample = 1 ms


class FakeSink:
    """Stands in for PyAudioSink. `on_write(n)` lets a test act after the
    n-th block of speech (the lead-in silence isn't counted), e.g. press
    stop right as sentence one ends."""

    def __init__(self, log, on_write=None):
        self.log = log
        self.on_write = on_write
        self.writes = 0

    def open(self, rate):
        self.log.append(("open", rate))

    def write(self, samples):
        if not len(samples) or samples[0] == 0.0:
            return  # lead-in silence
        self.writes += 1
        self.log.append(("write", samples[0]))
        if self.on_write:
            self.on_write(self.writes)

    def finish(self):
        self.log.append(("finish",))

    def abort(self):
        self.log.append(("abort",))


class StreamingFixture(unittest.TestCase):
    """Kokoro faked as available; each sentence's samples carry its index,
    so what reached the speakers can be read back from the log."""

    stream_env = "1"
    engine = "kokoro"

    def setUp(self):
        self.log = []
        self.synthesized = []
        fake = types.ModuleType("tts_kokoro")
        fake.available = lambda: True

        def synth(sentence):
            self.synthesized.append(sentence)
            return [float(len(self.synthesized))] * 120, RATE  # 120 ms each

        fake.synthesize_samples = synth
        self.sink = FakeSink(self.log)
        self.tmp = tempfile.TemporaryDirectory()
        self.latency_log = os.path.join(self.tmp.name, "lat.jsonl")
        self.inworld = Mock(return_value=None)
        patches = [
            patch.dict(sys.modules, {"tts_kokoro": fake}),
            patch.dict(os.environ, {"DENISSE_TTS_STREAM": self.stream_env}),
            patch.object(voice_output, "TTS_ENGINE", self.engine),
            patch.object(voice_output, "_stream_sink", self.sink),
            patch.object(vp, "LATENCY_LOG", self.latency_log),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def written(self):
        return [e[1] for e in self.log if e[0] == "write"]

    def latency_rows(self):
        return vp.read_log(self.latency_log)


# -- tray chat ----------------------------------------------------------------

class TrayFixture(StreamingFixture):
    def setUp(self):
        super().setUp()
        self.popup = tray.ChatPopup.__new__(tray.ChatPopup)
        self.reveals = []

        def reveal(reply, audio, play_now):
            self.reveals.append(reply)
            self.log.append(("reveal", reply))
            if play_now is not None:
                play_now.set()

        self.popup._reveal_reply = reveal
        root = Mock()
        root.after.side_effect = lambda delay, callback: callback()
        for p in (patch.object(tray, "root", root, create=True),
                  patch.object(tray, "synthesize_inworld", self.inworld),
                  patch.object(tray, "prepare_audio", Mock(return_value=None))):
            p.start()
            self.addCleanup(p.stop)

    def run_turn(self, reply="First one. Second one. Third one."):
        trace = vp.LatencyTrace()
        trace.mark("input_end")
        trace.extra["source"] = "tray_chat"
        trace.mark("first_token")
        self.popup._synthesize_then_reveal(reply, speech_control.capture(), trace)
        return trace


class TrayStreams(TrayFixture):
    def test_kokoro_streams_every_sentence_without_mci(self):
        self.run_turn()
        self.inworld.assert_not_called()
        self.assertEqual(self.synthesized, ["First one.", "Second one.", "Third one."])
        self.assertEqual(sorted(set(self.written())), [1.0, 2.0, 3.0])
        self.assertIn(("finish",), self.log)

    def test_bubble_revealed_once_at_first_audio(self):
        self.run_turn()
        self.assertEqual(self.reveals, ["First one. Second one. Third one."])
        reveal_at = self.log.index(("reveal", "First one. Second one. Third one."))
        first_write = next(i for i, e in enumerate(self.log) if e[0] == "write")
        open_at = self.log.index(("open", RATE))
        self.assertLess(open_at, reveal_at)      # audio is ready ...
        self.assertLess(reveal_at, first_write)  # ... the text shows, then she talks

    def test_stop_between_sentences(self):
        # 120 ms sentences in 50 ms blocks: blocks 1-3 are sentence one.
        self.sink.on_write = lambda n: n == 3 and speech_control.stop()
        trace = self.run_turn()
        self.assertEqual(set(self.written()), {1.0})
        self.assertIn(("abort",), self.log)
        self.assertNotIn(("finish",), self.log)
        self.assertTrue(trace.extra["interrupted"])
        self.assertEqual(trace.extra["sentences"], 1)
        self.assertEqual(len(self.reveals), 1)
        self.inworld.assert_not_called()  # a stop is not a reason to fall back

    def test_stopped_before_first_audio_still_shows_text_once(self):
        self.popup._reveal_reply = Mock(side_effect=lambda r, a, p: p and p.set())
        epoch = speech_control.capture()
        orig = sys.modules["tts_kokoro"].synthesize_samples

        def synth_then_stop(sentence):
            speech_control.stop()
            return orig(sentence)

        sys.modules["tts_kokoro"].synthesize_samples = synth_then_stop
        self.popup._synthesize_then_reveal("One. Two.", epoch, vp.LatencyTrace())
        self.popup._reveal_reply.assert_called_once()
        self.assertEqual(self.written(), [])
        self.inworld.assert_not_called()

    def test_latency_trace_written_per_turn(self):
        self.run_turn()
        self.run_turn("Just one.")
        rows = self.latency_rows()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["source"], "tray_chat")
            self.assertEqual(row["tts"], "kokoro-stream")
            for stage in ("first_token", "first_sentence", "tts_first_chunk", "playback_start"):
                self.assertIn(stage, row["deltas"])

    def test_nothing_synthesized_falls_back_to_mci(self):
        sys.modules["tts_kokoro"].synthesize_samples = lambda s: None
        self.run_turn("One. Two.")
        self.inworld.assert_called_once_with("One. Two.")
        self.assertEqual(self.reveals, ["One. Two."])


class TrayFallsBackWhenStreamOff(TrayFixture):
    stream_env = "0"

    def test_uses_whole_reply_path(self):
        self.run_turn("One. Two.")
        self.inworld.assert_called_once_with("One. Two.")
        self.assertEqual(self.log, [("reveal", "One. Two.")])  # the sink never opened
        self.assertEqual(self.latency_rows(), [])  # nothing was spoken


class TrayFallsBackForInworld(TrayFixture):
    engine = "inworld"

    def test_inworld_keeps_mci_path(self):
        audio = Mock()
        with patch.object(tray, "synthesize_inworld", Mock(return_value="x.mp3")) as synth, \
             patch.object(tray, "prepare_audio", Mock(return_value=audio)):
            self.run_turn("One. Two.")
        synth.assert_called_once_with("One. Two.")
        audio.play.assert_called_once()
        audio.close.assert_called_once()
        self.assertEqual(self.written(), [])
        self.assertEqual(self.latency_rows()[0]["tts"], "mci")


class TrayFallsBackWithoutKokoro(TrayFixture):
    def test_kokoro_missing(self):
        sys.modules["tts_kokoro"].available = lambda: False
        self.run_turn("One.")
        self.inworld.assert_called_once_with("One.")
        self.assertEqual(self.written(), [])


# -- wake word ----------------------------------------------------------------

class WakeFixture(StreamingFixture):
    def setUp(self):
        super().setUp()
        self.events = []
        self.brain = Mock()
        self.brain.respond.side_effect = lambda messages, **kw: (
            self.reply, messages + [{"role": "assistant", "content": self.reply}])
        self.reply = "First one. Second one. Third one."
        for p in (patch.object(wake_word.conversation, "replay", Mock(return_value=[])),
                  patch.object(wake_word.conversation, "record", Mock(return_value=True)),
                  patch.object(wake_word, "synthesize_inworld", self.inworld),
                  patch.object(wake_word, "prepare_audio", Mock(return_value=None)),
                  patch.object(wake_word, "_recent_context_text", Mock(return_value=""))):
            p.start()
            self.addCleanup(p.stop)

    def run_command(self, command="what's up"):
        gen = wake_word._start_new_request()
        wake_word._process_command(
            command, gen, self.brain, None,
            on_exchange=lambda c, r: self.events.append(("exchange", r)),
            on_thinking=lambda on: self.events.append(("thinking", on)),
            on_reply=lambda r: (self.events.append(("reply", r)), self.log.append(("reveal", r))))
        return gen


class WakeWordStreams(WakeFixture):
    def test_kokoro_streams_and_reveals_once_at_first_audio(self):
        self.run_command()
        self.inworld.assert_not_called()
        self.assertEqual(sorted(set(self.written())), [1.0, 2.0, 3.0])
        self.assertEqual(self.events, [("thinking", True), ("reply", self.reply),
                                       ("thinking", False), ("exchange", self.reply)])
        reveal_at = self.log.index(("reveal", self.reply))
        first_write = next(i for i, e in enumerate(self.log) if e[0] == "write")
        self.assertLess(reveal_at, first_write)

    def test_stop_word_between_sentences(self):
        self.sink.on_write = lambda n: n == 3 and speech_control.stop()
        self.run_command()
        self.assertEqual(set(self.written()), {1.0})
        self.assertIn(("abort",), self.log)
        self.inworld.assert_not_called()
        self.assertEqual([e for e in self.events if e[0] == "reply"], [("reply", self.reply)])

    def test_newer_request_stops_it_between_sentences(self):
        self.sink.on_write = lambda n: n == 3 and wake_word._start_new_request()
        self.run_command()
        self.assertEqual(set(self.written()), {1.0})
        self.assertNotIn("exchange", [e[0] for e in self.events])  # superseded: dropped
        self.inworld.assert_not_called()

    def test_interrupt_event_stops_it(self):
        def interrupt_current(n):
            if n == 3:
                with wake_word._state_lock:
                    wake_word._active_interrupt.set()
        self.sink.on_write = interrupt_current
        self.run_command()
        self.assertEqual(set(self.written()), {1.0})
        self.assertIsNone(wake_word._active_interrupt)  # unregistered afterwards

    def test_latency_trace_written(self):
        self.run_command()
        rows = self.latency_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "wake_word")
        self.assertEqual(rows[0]["tts"], "kokoro-stream")
        self.assertEqual(rows[0]["sentences"], 3)
        for stage in ("speech_end", "stt_done", "first_token", "tts_first_chunk", "playback_start"):
            self.assertIn(stage, rows[0]["deltas"])


class WakeWordFallsBack(WakeFixture):
    stream_env = "0"

    def test_stream_off_uses_whole_reply_path(self):
        self.run_command()
        self.inworld.assert_called_once_with(self.reply)
        self.assertEqual(self.written(), [])
        self.assertEqual(self.events, [("thinking", True), ("reply", self.reply),
                                       ("thinking", False), ("exchange", self.reply)])

    def test_mci_turn_is_traced(self):
        audio = Mock()
        with patch.object(wake_word, "synthesize_inworld", Mock(return_value="x.wav")), \
             patch.object(wake_word, "prepare_audio", Mock(return_value=audio)):
            self.run_command()
        audio.play.assert_called_once()
        rows = self.latency_rows()
        self.assertEqual(rows[0]["tts"], "mci")
        self.assertEqual(rows[0]["source"], "wake_word")


class WakeWordFallsBackForInworld(WakeFixture):
    engine = "inworld"

    def test_inworld_keeps_old_path(self):
        self.run_command()
        self.inworld.assert_called_once_with(self.reply)
        self.assertEqual(self.written(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
