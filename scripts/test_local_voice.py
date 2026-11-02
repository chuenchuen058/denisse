"""Fallback order for the voice: Kokoro / Inworld / pyttsx3, and the STT switch.

    .venv\\Scripts\\python.exe scripts\\test_local_voice.py
No audio is produced; the engines are replaced with fakes.
"""
import importlib
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load_voice_output(engine, kokoro_path, inworld_path):
    os.environ["DENISSE_TTS"] = engine
    fake = types.ModuleType("tts_kokoro")
    fake.synthesize = lambda text, path=None: kokoro_path
    sys.modules["tts_kokoro"] = fake
    import voice_output
    vo = importlib.reload(voice_output)
    vo._synthesize_inworld_only = lambda text: inworld_path
    return vo


class VoiceOrder(unittest.TestCase):
    def test_kokoro_first_by_default(self):
        vo = load_voice_output("kokoro", "k.wav", "i.mp3")
        self.assertEqual(vo.synthesize_inworld("hi"), "k.wav")

    def test_kokoro_failing_falls_back_to_inworld(self):
        vo = load_voice_output("kokoro", None, "i.mp3")
        self.assertEqual(vo.synthesize_inworld("hi"), "i.mp3")

    def test_inworld_first_when_chosen_then_kokoro(self):
        vo = load_voice_output("inworld", "k.wav", "i.mp3")
        self.assertEqual(vo.synthesize_inworld("hi"), "i.mp3")
        vo = load_voice_output("inworld", "k.wav", None)
        self.assertEqual(vo.synthesize_inworld("hi"), "k.wav")

    def test_both_failing_returns_none_so_pyttsx3_speaks(self):
        vo = load_voice_output("kokoro", None, None)
        self.assertIsNone(vo.synthesize_inworld("hi"))
        self.assertIsNone(vo.synthesize_inworld(""))

    def tearDown(self):
        sys.modules.pop("tts_kokoro", None)
        os.environ.pop("DENISSE_TTS", None)


class SttSwitch(unittest.TestCase):
    def test_whisper_failure_falls_back_to_vosk(self):
        os.environ["DENISSE_STT"] = "whisper"
        fake_w = types.ModuleType("stt_whisper")
        fake_w.is_ready = lambda: True
        def boom(**kw):
            raise RuntimeError("no model")
        fake_w.transcribe_once = boom
        sys.modules["stt_whisper"] = fake_w
        import voice_mode
        vm = importlib.reload(voice_mode)
        vm.stt_vosk.is_ready = lambda: True
        vm.stt_vosk.transcribe_once = lambda **kw: " from vosk "
        self.assertEqual(vm.listen(), "from vosk")

    def test_whisper_used_when_it_works(self):
        os.environ["DENISSE_STT"] = "whisper"
        fake_w = types.ModuleType("stt_whisper")
        fake_w.is_ready = lambda: True
        fake_w.transcribe_once = lambda **kw: " from whisper "
        sys.modules["stt_whisper"] = fake_w
        import voice_mode
        vm = importlib.reload(voice_mode)
        self.assertEqual(vm.listen(), "from whisper")

    def tearDown(self):
        sys.modules.pop("stt_whisper", None)
        os.environ.pop("DENISSE_STT", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
