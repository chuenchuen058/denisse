"""The speech engines read voice.json (AGENTS.md Rule 0): stt_vosk, stt_whisper
and tts_kokoro ship with exactly the values they used to hardcode, changing
the data changes what they do, and the existing env overrides still win.
No microphone, speakers, model files or downloads -- the models are fakes.

    .venv\\Scripts\\python.exe scripts\\test_speech_engines_config.py
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import voice_config  # noqa: E402

_ENV = ("DENISSE_VOICE_CONFIG", "DENISSE_VOSK_MODEL_PATH", "DENISSE_WAKE_MODEL_PATH",
        "DENISSE_VOSK_NOISE_GATE_MULTIPLIER", "DENISSE_WHISPER_DEVICE", "DENISSE_KOKORO_DIR",
        "DENISSE_KOKORO_VOICE", "DENISSE_KOKORO_SPEED", "DENISSE_KOKORO_DEVICE")
_MODULES = ("stt_vosk", "stt_whisper", "tts_kokoro")


class _Engines(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {})
        self._env.start()
        for k in _ENV:
            os.environ.pop(k, None)

    def tearDown(self):
        self._env.stop()
        voice_config.reload()
        for name in _MODULES:  # back to the shipped file for every later test
            if name in sys.modules:
                importlib.reload(sys.modules[name])
        self._tmp.cleanup()

    def load(self, overrides=None, env=None):
        """(stt_vosk, stt_whisper, tts_kokoro) reloaded against `overrides`."""
        if overrides is not None:
            path = Path(self._tmp.name) / "voice.json"
            path.write_text(json.dumps(overrides), encoding="utf-8")
            os.environ["DENISSE_VOICE_CONFIG"] = str(path)
        os.environ.update(env or {})
        voice_config.reload()
        return tuple(importlib.reload(importlib.import_module(n)) for n in _MODULES)


class ShippedDefaultsAreTheOldValues(_Engines):
    def test_stt_vosk(self):
        vosk_, _w, _k = self.load()
        self.assertEqual((vosk_.SAMPLE_RATE, vosk_.CHUNK_FRAMES, vosk_.MAX_ALTERNATIVES), (16000, 2000, 5))
        self.assertEqual((vosk_.NOISE_GATE_MULTIPLIER, vosk_.NOISE_GATE_MIN_FLOOR), (5.0, 30))
        self.assertEqual((vosk_.NOISE_FLOOR_WINDOW_S, vosk_.NOISE_FLOOR_MIN_CHUNKS,
                          vosk_.NOISE_FLOOR_PERCENTILE), (12.0, 8, 0.2))
        self.assertEqual(json.loads(vosk_.WAKE_GRAMMAR),
                         ["denisse", "denise", "hey denisse", "hey denise", "hi denisse", "hi denise", "[unk]"])
        self.assertEqual(vosk_.MODEL_PATH, str(ROOT / "models" / "vosk-model-en-us-0.22-lgraph"))
        self.assertEqual(vosk_.WAKE_MODEL_PATH, vosk_.MODEL_PATH)
        self.assertEqual((vosk_.DECODE_POLL_S, vosk_.STOP_JOIN_S), (0.5, 2.0))
        self.assertEqual(vosk_._listen_limits(None, None), (8.0, 15.0))
        # the old inline floor: 20th percentile (index int(n*0.2)-1), clamped at 30
        levels = list(range(10, 400, 10))
        old = max(30, sorted(levels)[max(0, int(len(levels) * 0.2) - 1)])
        self.assertEqual(vosk_._noise_floor(levels), old)
        self.assertEqual(vosk_._noise_floor([1] * 8), 30)

    def test_stt_whisper(self):
        _v, w, _k = self.load()
        self.assertEqual((w.DEVICE, w.CHUNK_FRAMES, w.LEAD_IN_CHUNKS), ("auto", 1024, 4))
        self.assertEqual((w.LANGUAGE, w.BEAM_SIZE, w.VAD_FILTER), ("en", 5, True))
        self.assertEqual(w.SAMPLE_RATE, 16000)

    def test_tts_kokoro(self):
        _v, _w, k = self.load()
        self.assertEqual(k.MODEL_PATH, str(ROOT / "models" / "kokoro" / "kokoro-v1.0.onnx"))
        self.assertEqual(k.VOICES_PATH, str(ROOT / "models" / "kokoro" / "voices-v1.0.bin"))
        self.assertEqual((k.VOICE, k.SPEED, k.LANG, k.DEVICE), ("af_heart", 1.0, "en-us", "auto"))
        self.assertEqual(k.WARM_UP_TEXT, "Ready.")


class DataChangesBehaviour(_Engines):
    def test_vosk_rates_gate_grammar_and_model_come_from_data(self):
        model_dir = Path(self._tmp.name) / "other-model"
        vosk_, w, _k = self.load({"stt_vosk": {
            "sample_rate": 8000, "chunk_frames": 800, "max_alternatives": 2,
            "noise_gate_multiplier": 2.5, "noise_gate_min_floor": 5, "noise_floor_percentile": 0.5,
            "noise_floor_min_chunks": 4, "wake_grammar": ["jarvis", "hey jarvis"],
            "model_dir": str(model_dir)},
            "voice_mode": {"listen_timeout_s": 3, "phrase_time_limit_s": 4}})
        self.assertEqual((vosk_.SAMPLE_RATE, vosk_.CHUNK_FRAMES, vosk_.MAX_ALTERNATIVES), (8000, 800, 2))
        self.assertEqual(vosk_.NOISE_GATE_MULTIPLIER, 2.5)
        self.assertEqual(json.loads(vosk_.WAKE_GRAMMAR), ["jarvis", "hey jarvis", "[unk]"])
        self.assertEqual(vosk_.MODEL_PATH, str(model_dir))  # absolute paths are taken as written
        self.assertEqual(vosk_._noise_floor([10, 20, 30, 40, 50, 60, 70, 80]), 40)  # the median now, not the 20th percentile
        self.assertEqual(vosk_._noise_floor([1, 1, 1, 1]), 5)
        self.assertEqual(vosk_._listen_limits(None, None), (3, 4))
        self.assertEqual(vosk_._listen_limits(1, 2), (1, 2))  # a caller's own limits still win
        self.assertEqual(w.SAMPLE_RATE, 8000)  # whisper records at Vosk's rate
        with self.assertRaises(vosk_.ModelUnavailable) as err:
            vosk_._get_model()
        self.assertIn("other-model", str(err.exception))

    def test_env_overrides_still_win(self):
        vosk_, w, k = self.load({"stt_vosk": {"noise_gate_multiplier": 2.5},
                                 "stt_whisper": {"device": "cuda"},
                                 "tts_kokoro": {"voice": "af_bella", "speed": 1.5}},
                                env={"DENISSE_VOSK_NOISE_GATE_MULTIPLIER": "7", "DENISSE_WHISPER_DEVICE": "cpu",
                                     "DENISSE_KOKORO_VOICE": "bf_emma", "DENISSE_KOKORO_SPEED": "0.9"})
        self.assertEqual(vosk_.NOISE_GATE_MULTIPLIER, 7.0)
        self.assertEqual(w.DEVICE, "cpu")
        self.assertEqual((k.VOICE, k.SPEED), ("bf_emma", 0.9))

    def test_whisper_decoding_options_come_from_data(self):
        _v, w, _k = self.load({"stt_whisper": {"language": "fr", "beam_size": 2, "vad_filter": False,
                                               "lead_in_chunks": 7, "device": "cpu"}})
        self.assertEqual((w.LEAD_IN_CHUNKS, w.DEVICE), (7, "cpu"))
        seen = {}

        class Seg:
            text = " bonjour "

        class Model:
            def transcribe(self, source, **kw):
                seen.update(kw)
                return [Seg()], None

        w._model = Model()
        self.assertEqual(w.transcribe_file("clip.wav"), "bonjour")
        self.assertEqual(seen, {"language": "fr", "beam_size": 2, "vad_filter": False})

    def test_kokoro_voice_files_and_wording_come_from_data(self):
        kdir = Path(self._tmp.name) / "voices"
        _v, _w, k = self.load({"tts_kokoro": {
            "model_dir": str(kdir), "model_file": "m.onnx", "voices_file": "v.bin",
            "voice": "bf_emma", "speed": 1.25, "lang": "en-gb", "warm_up_text": "Hello.",
            "unavailable_note": "no voice: {error}"}})
        self.assertEqual((k.MODEL_PATH, k.VOICES_PATH), (str(kdir / "m.onnx"), str(kdir / "v.bin")))
        self.assertFalse(k.available())  # files not there
        calls = []

        class Fake:
            def create(self, text, **kw):
                calls.append((text, kw))
                if text == "fail":
                    raise RuntimeError("boom")
                return [0.0], 24000

        k._kokoro = Fake()
        with patch.object(k, "available", lambda: True):
            self.assertEqual(k.synthesize_samples("hi"), ([0.0], 24000))
            with patch("builtins.print") as printed:
                self.assertIsNone(k.synthesize_samples("fail"))
            printed.assert_called_once_with("no voice: boom")
            with patch.object(k, "synthesize", lambda text: calls.append((text, "warm")) or None):
                k.warm_up()
        self.assertEqual(calls[0], ("hi", {"voice": "bf_emma", "speed": 1.25, "lang": "en-gb"}))
        self.assertEqual(calls[-1], ("Hello.", "warm"))

    def test_bad_engine_values_fall_back_to_the_defaults(self):
        with self.assertLogs("denisse.voice_config", level="WARNING"):
            vosk_, w, k = self.load({"stt_vosk": {"sample_rate": "fast", "wake_grammar": "denisse"},
                                     "stt_whisper": {"beam_size": 0, "vad_filter": "yes"},
                                     "tts_kokoro": {"speed": -1}})
        self.assertEqual(vosk_.SAMPLE_RATE, 16000)
        self.assertIn("denisse", json.loads(vosk_.WAKE_GRAMMAR))
        self.assertEqual((w.BEAM_SIZE, w.VAD_FILTER), (5, True))
        self.assertEqual(k.SPEED, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
