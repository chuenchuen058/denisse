"""Shared structured-output and existing JSON callers, without external I/O."""
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ollama
import ollama_output as output


class CompatibilityTests(unittest.TestCase):
    def test_inference_preserves_types_without_leaking_sample_answers(self):
        shape = output.schema_from_example({"string": "secret", "flag": False, "count": 15,
                                            "measure": 3.5, "missing": None, "empty": [],
                                            "mixed": ["value", 4], "rows": [{"nested": True}]})
        props = shape["properties"]
        self.assertEqual([props[k]["type"] for k in ("string", "flag", "count", "measure", "missing")],
                         ["string", "boolean", "integer", "number", "null"])
        self.assertEqual(props["empty"]["items"], {})
        self.assertEqual(props["mixed"]["items"], {"anyOf": [{"type": "string"}, {"type": "integer"}]})
        self.assertEqual(props["rows"]["items"]["required"], ["nested"])
        self.assertEqual(set(shape["required"]), set(props))
        self.assertNotIn("secret", json.dumps(shape))
        self.assertNotIn("const", json.dumps(shape))
        self.assertNotIn("enum", json.dumps(shape))

    def test_http_rejection_retries_once_with_identical_non_format_arguments(self):
        err = HTTPError("http://fake/api/chat", 400, "Bad Request", {},
                        io.BytesIO(b'{"error":"cannot unmarshal object into field format of type string"}'))
        call = Mock(side_effect=[err, "answer"])
        got = output.call_with_schema(call, schema={"type": "object"}, enabled=True,
                                      think=False, options={"temperature": 0.2})
        self.assertEqual(got, "answer")
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args.kwargs, {"think": False, "options": {"temperature": 0.2}})

    def test_unrelated_errors_propagate_without_extra_call(self):
        errors = [ollama.ResponseError("model not found", 404),
                  ollama.ResponseError("invalid options", 400),
                  ollama.ResponseError("invalid model information", 400),
                  ollama.ResponseError("format processing ran out of memory", 500),
                  TimeoutError("slow server"), ConnectionError("server down"),
                  TypeError("bug inside caller")]
        for error in errors:
            with self.subTest(error=error):
                call = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    output.call_with_schema(call, schema={}, enabled=True)
                self.assertEqual(call.call_count, 1)

    def test_failed_compatibility_retry_is_not_retried_again(self):
        call = Mock(side_effect=ollama.ResponseError("format unsupported", 400))
        with self.assertRaises(ollama.ResponseError):
            output.call_with_schema(call, schema={}, enabled=True)
        self.assertEqual(call.call_count, 2)

    def test_unrelated_http_error_keeps_body_available_to_caller(self):
        body = b'{"error":"invalid model information"}'
        error = HTTPError("http://fake", 400, "Bad Request", {}, io.BytesIO(body))
        call = Mock(side_effect=error)
        with self.assertRaises(HTTPError) as caught:
            output.call_with_schema(call, schema={}, enabled=True)
        self.assertIs(caught.exception, error)
        self.assertEqual(caught.exception.read(), body)
        call.assert_called_once()

    def test_disabled_and_non_json_calls_never_send_format(self):
        for enabled, schema in ((False, {}), (True, None)):
            call = Mock(return_value="text")
            self.assertEqual(output.call_with_schema(call, schema=schema, enabled=enabled, think=False), "text")
            self.assertEqual(call.call_args.kwargs, {"think": False})


class ExistingCallersTests(unittest.TestCase):
    def setUp(self):
        # Importing the real brain would load unrelated machine/memory state.
        self.brain = SimpleNamespace(MODEL="qwen3.5:9b", NUM_CTX=8192,
            CONFIG={"chat": {"keep_alive": -1}}, _chat_extra=lambda: {"think": False},
            _get=lambda obj, key, default=None: obj.get(key, default), ollama=SimpleNamespace(chat=None))
        p = patch.dict(sys.modules, {"brain": self.brain})
        p.start()
        self.addCleanup(p.stop)

    def test_multistep_schema_switch_and_rejection(self):
        import multistep
        request = "find my notes and list my tasks"
        response = {"message": {"content": '{"steps": ["find my notes", "list my tasks"]}'}}
        for mode in ("on", "off", "reject"):
            responses = [ollama.ResponseError("format unsupported", 400), response] if mode == "reject" else [response]
            chat = Mock(side_effect=responses)
            with self.subTest(mode=mode), patch.dict(multistep._R, {"structured_output": mode != "off"}):
                self.assertEqual(multistep.make_plan(request, chat), ["find my notes", "list my tasks"])
            self.assertEqual(chat.call_count, len(responses))
            if mode != "off":
                self.assertEqual(chat.call_args_list[0].kwargs["format"], multistep._PLAN_SCHEMA)
            if mode != "on":
                self.assertNotIn("format", chat.call_args.kwargs)
            self.assertIs(chat.call_args.kwargs["think"], False)

    def test_review_agent_schema_switch_rejection_and_dynamic_ids(self):
        import agent_loop
        answer = {"tool": "finish", "note": "Review the observed item", "focus_ids": ["observed"]}
        response = SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer), thinking="reasoning"))
        messages = [{"role": "system", "content": "Synthetic review instructions"},
                    {"role": "user", "content": agent_loop.CONFIG["observation_prefix"] +
                     json.dumps({"tasks": [{"id": "observed"}]})}]
        for mode in ("on", "off", "reject"):
            responses = [ollama.ResponseError("format unsupported", 400), response] if mode == "reject" else [response]
            with self.subTest(mode=mode), patch("ollama.Client") as client, \
                 patch.dict(agent_loop.CONFIG, {"structured_output": mode != "off"}):
                client.return_value.chat.side_effect = responses
                self.assertEqual(agent_loop.LocalModel()(messages, ("finish",)), answer)
                calls = client.return_value.chat.call_args_list
            self.assertEqual(len(calls), len(responses))
            if mode != "off":
                schema = calls[0].kwargs["format"]
                self.assertEqual(schema["properties"]["tool"]["enum"], ["finish"])
                self.assertEqual(schema["properties"]["focus_ids"]["items"]["enum"], ["observed"])
            if mode != "on":
                self.assertNotIn("format", calls[-1].kwargs)
            self.assertIs(calls[-1].kwargs["think"], False)
            sent = calls[-1].kwargs["messages"]
            self.assertEqual([m["role"] for m in sent], ["system", "user"])
            self.assertIn("Synthetic review instructions", sent[0]["content"])
            self.assertIn('"focus_ids"', sent[0]["content"])
        self.assertEqual(messages[0]["content"], "Synthetic review instructions")
        self.assertEqual(len(messages), 2)
        shape = json.loads(Path(agent_loop.housekeeping.path(agent_loop.CONFIG["schema_file"])).read_text())
        self.assertNotIn("enum", shape["properties"]["tool"])


if __name__ == "__main__":
    unittest.main()
