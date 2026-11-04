"""Second softcode pass (2026-11-04): what multistep.py and clarify.py say,
and their limits, come from dialogue_rules.json. With the shipped file the
words are the ones the code used to hardcode; with an edited copy
(DENISSE_DIALOGUE_RULES) they change. Synthetic text; no model, no tools.

    .venv\\Scripts\\python.exe scripts\\test_dialogue_rules_data.py
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import clarify  # noqa: E402
import multistep  # noqa: E402

_OPTS = [{"label": f"{w} lab session", "text": f"{w} lab session: {d} November.", "title": "Labs"}
         for w, d in (("Electronics", 4), ("Optics", 11), ("Robotics", 25), ("Signals", 2), ("Power", 9))]


class ShippedDataSaysWhatTheCodeUsedTo(unittest.TestCase):
    """Pinned to the strings hardcoded before this pass: behaviour identical."""

    def test_clarify_wording(self):
        self.assertEqual(clarify.question(_OPTS[:1]), "Do you mean the electronics lab session?")
        self.assertEqual(clarify.question(_OPTS),
                         "Which one do you mean: the electronics lab session, the optics lab session, "
                         "the robotics lab session or the signals lab session?")
        self.assertEqual(clarify.missing("", _OPTS[:1]),
                         "The only match I found is the electronics lab session, and that isn't the one "
                         "you mean. I haven't found another, and I won't guess -- what is it called, "
                         "or where did you note it?")
        self.assertTrue(clarify.missing("labs", _OPTS[:2]).startswith(
            "For labs I found only the electronics lab session and the optics lab session, "
            "and none of those is the one you mean. I haven't"))
        self.assertEqual(clarify.quote(_OPTS[0]),
                         "Here's what I found for the electronics lab session: Electronics lab session: 4 November.")
        self.assertEqual(clarify._segments("From your notes:\n- a thing."), [("", "a thing.")])

    def test_multistep_wording(self):
        self.assertEqual(multistep.plain("User declined the call"), "you said no to it")
        self.assertEqual(multistep.plain(""), "the tool failed")
        note = multistep._step_note(2, 3, "do x and y", ["1. x -> ok"], "boom", ["25"], "FILE")
        self.assertEqual(note, "He asked: \"do x and y\". This is step 2 of 3; the other steps are handled "
                               "separately, so do only this one now and answer it in a sentence or two."
                               "\nHis exact details for this step (keep them as he said them): 25"
                               "\nEarlier steps (already done -- don't redo them):\n1. x -> ok"
                               "\nFILE"
                               "\nYour last try at this step failed: boom\nThat exact call is blocked now. "
                               "Do it a different way, or tell him plainly that you couldn't.")
        self.assertTrue(multistep._PLAN_PROMPT.startswith("Chuen asked for several things at once."))
        self.assertEqual(multistep._PLAN_SCHEMA["properties"]["steps"]["items"]["maxLength"], 200)
        calls = [{"name": "list_tasks", "arguments": {}, "result": "- Spritzer paperwork (due Thu 5 Nov)"}]
        self.assertEqual(multistep.keep_task_names("Done.", calls), "Done.\n\nAlso on your list: Spritzer paperwork.")


class EditedDataChangesBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = json.loads((ROOT / "dialogue_rules.json").read_text(encoding="utf-8"))
        m, c = data["multistep"], data["clarify"]
        m["said_no"]["value"] = "you turned it down"
        m["tool_failed"]["value"] = "it broke"
        m["step_note"]["value"] = "STEP {i}/{n} of <{request}>"
        m["step_note_retry"]["value"] = " RETRY({error})"
        m["file_note"]["value"] = "[{path}] {text}"
        m["also_on_list"]["value"] = "Still to do: {names}!"
        m["step_max_chars"]["value"] = 10
        m["plan_num_predict"]["value"] = 99
        m["plan_prompt"]["value"] = "PLAN IT"
        m["plan_user_message"]["value"] = "R={request} P={parts}"
        m["switch_off_values"]["value"] = ["nah"]
        c["ask_one"]["value"] = "{name}, right?"
        c["ask_many"]["value"] = "Pick: {names} / {last}"
        c["max_options_named"]["value"] = 2
        c["article"]["value"] = "your "
        c["quote"]["value"] = "{name} => {evidence}"
        c["missing_tail"]["value"] = " Tell me more."
        c["skip_line_starts"]["value"] = ["HEADER"]
        c["label_max_words"]["value"] = 1
        cls._dir = tempfile.TemporaryDirectory()
        path = Path(cls._dir.name) / "rules.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        cls._old = os.environ.get("DENISSE_DIALOGUE_RULES")
        os.environ["DENISSE_DIALOGUE_RULES"] = str(path)
        cls.ms, cls.cl = importlib.reload(multistep), importlib.reload(clarify)

    @classmethod
    def tearDownClass(cls):
        if cls._old is None:
            os.environ.pop("DENISSE_DIALOGUE_RULES", None)
        else:
            os.environ["DENISSE_DIALOGUE_RULES"] = cls._old
        importlib.reload(multistep)
        importlib.reload(clarify)
        cls._dir.cleanup()

    def test_multistep_sentences(self):
        ms = self.ms
        self.assertEqual(ms.plain("User declined"), "you turned it down")
        self.assertEqual(ms.plain(""), "it broke")
        self.assertEqual(ms._step_note(1, 2, "a and b", [], "oops"), "STEP 1/2 of <a and b> RETRY(oops)")
        self.assertEqual(ms._file_note("C:/x.md", "hi"), "[C:/x.md] hi")
        calls = [{"name": "list_tasks", "arguments": {}, "result": "- Spritzer paperwork"}]
        self.assertEqual(ms.keep_task_names("", calls), "Still to do: Spritzer paperwork!")

    def test_multistep_limits_and_switch(self):
        ms = self.ms
        self.assertEqual(ms._PLAN_SCHEMA["properties"]["steps"]["items"]["maxLength"], 10)
        self.assertIsNone(ms.parse_plan({"steps": ["find my cv now", "remind me"]}))
        self.assertEqual(ms.parse_plan({"steps": ["find cv", "remind me"]}), ["find cv", "remind me"])
        old = os.environ.get("DENISSE_PLANNER")
        try:
            os.environ["DENISSE_PLANNER"] = "nah"
            self.assertFalse(ms.enabled())
            os.environ["DENISSE_PLANNER"] = "0"
            self.assertTrue(ms.enabled())   # "0" is no longer an off value in this data
        finally:
            if old is None:
                os.environ.pop("DENISSE_PLANNER", None)
            else:
                os.environ["DENISSE_PLANNER"] = old

    def test_plan_call_uses_the_data(self):
        seen = {}

        def chat(**kw):
            seen.update(kw)
            raise RuntimeError("no model in tests")

        self.assertIsNone(self.ms.make_plan("find my cv and remind me on Friday", chat=chat))
        self.assertEqual(seen["messages"][0]["content"], "PLAN IT")
        self.assertTrue(seen["messages"][1]["content"].startswith("R=find my cv and remind me on Friday P=1. "))
        self.assertEqual(seen["options"]["num_predict"], 99)

    def test_clarify_sentences(self):
        cl = self.cl
        self.assertEqual(cl.question(_OPTS[:1]), "your electronics lab session, right?")
        self.assertEqual(cl.question(_OPTS), "Pick: your electronics lab session / your optics lab session")
        self.assertEqual(cl.quote(_OPTS[0]), "your electronics lab session => Electronics lab session: 4 November.")
        self.assertTrue(cl.missing("labs", _OPTS[:1]).endswith(" Tell me more."))
        self.assertEqual(cl._segments("HEADER x\nFrom your notes:"), [("", "From your notes:")])
        self.assertEqual(cl._label("", "Dentist check-up soon", []), "Dentist")


if __name__ == "__main__":
    unittest.main()
