"""voice.json drives the voice path (AGENTS.md Rule 0): the shipped file
matches the documented defaults, bad values fall back with a log line and
never crash, and changing the data changes what each module does --
speech_control, voice_pipeline, wake_word, voice_output, voice_mode.
No microphone, speakers or model.
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

# Env overrides that would hide the file's values.
_ENV = ("DENISSE_VOICE_CONFIG", "DENISSE_END_SILENCE_S", "DENISSE_MIN_SPEECH_RMS", "DENISSE_BARGE_IN",
        "DENISSE_BARGE_IN_MIN_S", "DENISSE_BARGE_IN_FACTOR", "DENISSE_TTS_LEAD_IN_S",
        "DENISSE_VOICE_LATENCY_LOG", "DENISSE_INWORLD_VOICE", "DENISSE_INWORLD_MODEL", "DENISSE_TTS",
        "DENISSE_TTS_STREAM", "DENISSE_VOICE", "DENISSE_STT", "DENISSE_WAKE_ACK",
        "DENISSE_RECENT_CONTEXT_SECONDS")


def _strip_notes(d):
    if isinstance(d, dict):
        return {k: _strip_notes(v) for k, v in d.items() if not k.startswith("_") or k == "_default"}
    return d


class _WithConfig(unittest.TestCase):
    """Point DENISSE_VOICE_CONFIG at a temp file holding `overrides`, reload
    the modules named, and put everything back afterwards."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {})
        self._env.start()
        for k in _ENV:
            os.environ.pop(k, None)
        self._reloaded = []

    def tearDown(self):
        self._env.stop()
        voice_config.reload()
        for mod in self._reloaded:
            importlib.reload(mod)
        self._tmp.cleanup()

    def use(self, overrides, *modules):
        path = Path(self._tmp.name) / f"voice{len(self._reloaded)}.json"
        path.write_text(json.dumps(overrides), encoding="utf-8")
        os.environ["DENISSE_VOICE_CONFIG"] = str(path)
        voice_config.reload()
        out = []
        for name in modules:
            mod = importlib.import_module(name)
            mod = importlib.reload(mod)
            self._reloaded.append(mod)
            out.append(mod)
        return out[0] if len(out) == 1 else out


class LoaderTests(_WithConfig):
    def test_shipped_file_is_the_documented_defaults(self):
        shipped = json.loads((ROOT / "voice.json").read_text(encoding="utf-8"))
        self.assertEqual(_strip_notes(shipped), {k: v for k, v in voice_config.DEFAULTS.items()})

    def test_shipped_file_loads_without_warnings(self):
        voice_config.reload()
        with self.assertNoLogs("denisse.voice_config", level="WARNING"):
            voice_config.section("pipeline")

    def test_bad_values_log_and_fall_back(self):
        self.use({"pipeline": {"end_silence_s": "slow", "min_speech_rms": -4, "barge_in": "yes",
                               "max_sentence_chars": 12.5, "block_s": 0.1, "nonsense": 1},
                  "wake_word": {"greetings": "hey", "mode_phrases": {"offline": 3}},
                  "nosuch": {}})
        with self.assertLogs("denisse.voice_config", level="WARNING") as logs:
            pipe = voice_config.section("pipeline")
            wake = voice_config.section("wake_word")
        text = "\n".join(logs.output)
        for key in ("pipeline.end_silence_s", "pipeline.min_speech_rms", "pipeline.barge_in",
                    "pipeline.max_sentence_chars", "pipeline.nonsense", "wake_word.greetings",
                    "wake_word.mode_phrases", "nosuch"):
            self.assertIn(key, text)
        d = voice_config.DEFAULTS
        self.assertEqual(pipe["end_silence_s"], d["pipeline"]["end_silence_s"])
        self.assertEqual(pipe["min_speech_rms"], d["pipeline"]["min_speech_rms"])
        self.assertIs(pipe["barge_in"], False)
        self.assertEqual(pipe["max_sentence_chars"], d["pipeline"]["max_sentence_chars"])
        self.assertEqual(pipe["block_s"], 0.1)  # the good value next to them still applies
        self.assertEqual(wake["greetings"], d["wake_word"]["greetings"])

    def test_unreadable_or_missing_file_uses_defaults(self):
        path = Path(self._tmp.name) / "broken.json"
        path.write_text("{ not json", encoding="utf-8")
        os.environ["DENISSE_VOICE_CONFIG"] = str(path)
        voice_config.reload()
        with self.assertLogs("denisse.voice_config", level="WARNING"):
            self.assertEqual(voice_config.section("confirm"), voice_config.DEFAULTS["confirm"])
        os.environ["DENISSE_VOICE_CONFIG"] = str(Path(self._tmp.name) / "absent.json")
        voice_config.reload()
        with self.assertLogs("denisse.voice_config", level="WARNING"):
            self.assertEqual(voice_config.section("output"), voice_config.DEFAULTS["output"])

    def test_modules_still_import_with_a_bad_file(self):
        self.use({"pipeline": {"abbreviations": 5}, "wake_word": {"name_stem": None},
                  "output": {"warm_up_rate": "fast"}, "voice_mode": {"exit_phrases": [1]},
                  "stop_request": {"phrases": "stop"}},
                 "speech_control", "voice_pipeline", "voice_output", "voice_mode", "wake_word")
        import speech_control
        import wake_word
        self.assertTrue(speech_control.is_stop_request("stop talking"))
        self.assertEqual(wake_word._match_wake_word(["hey denisse what time is it"]), "what time is it")


class DataChangesBehaviour(_WithConfig):
    def test_speech_control_stop_words_come_from_data(self):
        sc = self.use({"stop_request": {"phrases": ["enough"], "lead_words": ["right"],
                                        "trail_words": ["thanks"], "typos": {"enuf": "enough"}}},
                      "speech_control")
        self.assertTrue(sc.is_stop_request("right, enough thanks"))
        self.assertTrue(sc.is_stop_request("enuf"))
        self.assertFalse(sc.is_stop_request("stop talking"))  # no longer in the data
        self.assertFalse(sc.is_stop_request("enough of the timer"))

    def test_speech_control_empty_phrase_list_means_nothing_is_a_stop(self):
        sc = self.use({"stop_request": {"phrases": []}}, "speech_control")
        self.assertFalse(sc.is_stop_request("stop"))
        self.assertFalse(sc.is_stop_request(""))

    def test_voice_pipeline_thresholds_and_abbreviations_come_from_data(self):
        vp = self.use({"pipeline": {"end_silence_s": 1.5, "min_speech_rms": 50, "noise_gate_factor": 10,
                                    "abbreviations": ["approx", "zz"], "max_sentence_chars": 20,
                                    "synth_ahead": 5, "barge_in": True}}, "voice_pipeline")
        self.assertEqual(vp.END_SILENCE_S, 1.5)
        self.assertTrue(vp.BARGE_IN)
        ep = vp.Endpointer(chunk_s=0.1)
        self.assertEqual(ep.end_silence_s, 1.5)
        for t in range(4):  # calibrate on a noise floor of 20 -> gate 200, not 60
            ep.push(20, t * 0.1)
        self.assertEqual(ep.gate, 200)
        self.assertEqual(vp.split_sentences("See zz. this one"), ["See zz. this one"])
        self.assertEqual(vp.split_sentences("Dr. Smith is here"), ["Dr.", "Smith is here"])
        self.assertTrue(all(len(s) <= 20 for s in vp.split_sentences("word " * 30)))
        self.assertEqual(vp.StreamingSpeaker(None, None).ahead, 5)

    def test_wake_word_name_answers_and_phrases_come_from_data(self):
        ww = self.use({"wake_word": {"name_stem": "jar", "name_extra_letters": 4,
                                     "name_false_positives": ["jargon"], "greetings": ["yo"],
                                     "misheard_names": ["char miss"],
                                     "mode_phrases": {"offline": ["go dark"]}, "exit_phrases": ["bye now"]},
                       "confirm": {"yes_words": ["aye", "affirm*"], "no_words": ["nay"],
                                   "answer_max_words": 2, "timeout_s": 3,
                                   "describe": {"_default": "use {tool} on {target}"},
                                   "arg_fallbacks": {"target": "it"}}},
                      "wake_word")
        self.assertEqual(ww._match_wake_word(["yo jarvis what time is it"]), "what time is it")
        self.assertIsNone(ww._match_wake_word(["hey denisse what time is it"]))
        self.assertIsNone(ww._match_wake_word(["that is jargon"]))
        self.assertEqual(ww._match_wake_word(["char miss play music"], trust_fallback=True), "play music")
        self.assertTrue(ww._classify_answer("aye"))
        self.assertTrue(ww._classify_answer("affirmative"))
        self.assertFalse(ww._classify_answer("nay"))
        self.assertIsNone(ww._classify_answer("yes"))
        self.assertIsNone(ww._classify_answer("aye aye captain"))  # over answer_max_words
        self.assertEqual(ww.CONFIRM_TIMEOUT, 3)
        self.assertEqual(ww._describe_action("open_app", {}), "use open_app on it")
        self.assertEqual(ww._mode_command("go  dark"), "offline")
        self.assertIsNone(ww._mode_command("offline mode"))
        self.assertTrue(ww._is_exit("Bye now"))
        self.assertFalse(ww._is_exit("quit"))

    def test_voice_output_voice_and_stream_switch_come_from_data(self):
        vo = self.use({"output": {"inworld_voice": "Nova", "inworld_model": "m-test",
                                  "tts_engine": "Inworld", "tts_stream": False}}, "voice_output")
        self.assertEqual((vo.INWORLD_VOICE, vo.INWORLD_MODEL, vo.TTS_ENGINE), ("Nova", "m-test", "inworld"))
        self.assertFalse(vo.stream_enabled())
        vo = self.use({"output": {"tts_engine": "kokoro", "tts_stream": False}}, "voice_output")
        self.assertFalse(vo.stream_enabled())  # kokoro, but streaming switched off in data

    def test_voice_mode_voice_exit_and_timeouts_come_from_data(self):
        vm = self.use({"voice_mode": {"voice_preference": "zira", "stt_engine": "Vosk",
                                      "exit_phrases": ["goodbye denisse"], "listen_timeout_s": 3}},
                      "voice_mode")
        self.assertEqual(vm.STT_ENGINE, "vosk")
        self.assertEqual(vm.LISTEN_TIMEOUT_S, 3)
        self.assertTrue(vm.is_exit("Goodbye  Denisse"))
        self.assertFalse(vm.is_exit("exit"))

        class V:
            def __init__(self, vid, name, gender=""):
                self.id, self.name, self.gender = vid, name, gender

        class Engine:
            voice = None

            def getProperty(self, _):
                return [V("1", "Microsoft David", "male"), V("2", "Microsoft Zira", "female")]

            def setProperty(self, _, value):
                self.voice = value

        engine = Engine()
        vm._select_voice(engine)
        self.assertEqual(engine.voice, "2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
