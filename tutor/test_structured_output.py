"""Ollama transport tests: synthetic replies only, no model or vault access."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ollama

from ollama_output import schema_from_example
from tutor import cards, conf, consult, don, exam, model, sources, teach


class StructuredOutputTests(unittest.TestCase):
    def setUp(self):
        for p in (patch("model_config.model", return_value="qwen3.5:9b"),
                  patch.object(conf, "num_ctx", return_value=8192),
                  patch.dict(model._M, {"structured_output": True, "think": False})):
            p.start()
            self.addCleanup(p.stop)

    def extract(self):
        return model.extract_lecture("Synthetic", "TEST", 1, [], "Some material.")[0]

    def test_extract_passes_schema_and_think_false_ignoring_separate_thinking(self):
        # A thinking-model-shaped response: reasoning must not replace content.
        response = SimpleNamespace(message=SimpleNamespace(
            thinking="Consider the lecture before answering.",
            content='{"outcomes": ["Explain the method"], "methods": [], "examples": [], "check": [], "topics": []}'))
        with patch("ollama.chat", return_value=response) as chat:
            self.assertEqual(self.extract()["outcomes"], ["Explain the method"])
        self.assertEqual(chat.call_count, 1)
        kw = chat.call_args.kwargs
        self.assertEqual(kw["format"], schema_from_example(json.loads(model._PR["schema"])))
        self.assertIs(kw["think"], False)
        self.assertEqual(kw["model"], "qwen3.5:9b")

    def test_server_rejection_retains_options_and_uses_tolerant_parser(self):
        response = {"message": {"content": 'Some preamble. ```json\n{"outcomes": ["Recovered"]}\n```'}}
        with patch("ollama.chat", side_effect=[ollama.ResponseError("invalid format: expected string", 400),
                                               response]) as chat:
            self.assertEqual(self.extract()["outcomes"], ["Recovered"])
        first, second = [c.kwargs for c in chat.call_args_list]
        self.assertIn("format", first)
        self.assertNotIn("format", second)
        self.assertEqual({k: v for k, v in first.items() if k != "format"}, second)
        self.assertIs(second["think"], False)

    def test_switch_off_still_parses_preamble_and_fences(self):
        with patch.dict(model._M, {"structured_output": False}), patch("ollama.chat", return_value={
                "message": {"content": '<think>consider this</think>\n```json\n{"outcomes": ["Legacy"]}\n```'}}) as chat:
            self.assertEqual(self.extract()["outcomes"], ["Legacy"])
        self.assertNotIn("format", chat.call_args.kwargs)
        self.assertIs(chat.call_args.kwargs["think"], False)

    def test_schema_tracks_changed_prompt_data(self):
        changed = '{"outcomes": ["..."], "new_field": {"usable": true, "score": 0.5}}'
        with patch.dict(model._PR, {"schema": changed}), patch("ollama.chat", return_value={
                "message": {"content": '{"outcomes": []}'}}) as chat:
            self.extract()
        schema = chat.call_args.kwargs["format"]
        self.assertEqual(set(schema["properties"]), {"outcomes", "new_field"})
        self.assertEqual(schema["properties"]["new_field"]["properties"]["usable"], {"type": "boolean"})
        self.assertIn(changed, chat.call_args.kwargs["messages"][-1]["content"])

    def test_json_retry_keeps_schema_and_retry_options(self):
        with patch.dict(model._M, {"chunk_retries": 1, "retry_options": [{"temperature": 0.7}]}), \
             patch("ollama.chat", side_effect=[{"message": {"content": "truncated"}},
                                               {"message": {"content": '{"outcomes": []}'}}]) as chat:
            self.extract()
        a, b = [c.kwargs for c in chat.call_args_list]
        self.assertEqual(a["format"], b["format"])
        self.assertEqual(b["options"]["temperature"], 0.7)
        self.assertIs(b["think"], False)

    def test_all_tutor_operations_pass_their_own_shape(self):
        cfg = conf.CONFIG
        with tempfile.TemporaryDirectory() as tmp:
            module = conf.Module(tmp, "TEST", "Synthetic")
            source = sources.Source(str(Path(tmp) / "note.md"), "note.md", "own", 1, "Topic", "Text")
            item = {"title": "Question", "marks": 2, "question": "Explain", "mark_scheme": [], "cards": []}
            operations = [
                (cfg["cards"]["schema"], lambda: cards.generate(module, source, only={"recall"})),
                (cfg["cards"]["check"]["schema"], lambda: cards._ask_checks(model.ollama_call, "check", "cards")),
                (cfg["teach"]["schema"], lambda: teach.ask("local", "teach", "topic")),
                (cfg["consult"]["schema"], lambda: consult.ask("local", "consult", "question")),
                (exam._Q["mark_schema"], lambda: exam.mark_quiz(module, item, "answer")),
                (exam._X["schema"], lambda: exam.set_exam(None, module, "Topic")),
                (exam._X["mark_schema"], lambda: exam.mark_exam(module, item, "answer")),
            ]
            with patch.object(cards, "engine_call", return_value=model.ollama_call), \
                 patch.object(exam, "style_text", return_value=("", [])), \
                 patch.object(teach, "material", return_value={"text": "Material", "links": []}), \
                 patch.object(exam, "load", return_value={}), patch.object(don, "save_json"):
                for shape, operation in operations:
                    with self.subTest(shape=shape), patch("ollama.chat", return_value={
                            "message": {"content": shape}}) as chat:
                        operation()
                        self.assertEqual(chat.call_count, 1)
                        self.assertEqual(chat.call_args.kwargs["format"], schema_from_example(json.loads(shape)))
                        self.assertIs(chat.call_args.kwargs["think"], False)

    def test_check_rejection_still_accepts_legacy_response(self):
        with patch("ollama.chat", side_effect=[ollama.ResponseError("format not supported", 400),
                   {"message": {"content": 'Here you go: {"checks": [{"n": 1, "verdict": "right"}]}'}}]):
            checks = cards._ask_checks(model.ollama_call, "Check", "Synthetic card")
        self.assertEqual(checks, [{"n": 1, "verdict": "right"}])

    def test_think_null_omits_argument_and_true_is_preserved(self):
        for think in (None, True):
            with self.subTest(think=think), patch.dict(model._M, {"think": think}), \
                 patch("ollama.chat", return_value={"message": {"content": "{}"}}) as chat:
                self.extract()
                if think is None:
                    self.assertNotIn("think", chat.call_args.kwargs)
                else:
                    self.assertIs(chat.call_args.kwargs["think"], True)


if __name__ == "__main__":
    unittest.main()
