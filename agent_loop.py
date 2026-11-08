"""Bounded plan/observe/check loop for unattended reviews, separate from chat.

Decisions are short action records, not hidden chain-of-thought. Every exposed
tool takes no model-supplied arguments; its scope is bound by the caller.

Budgets, model options and every word the model or the brief sees are data:
the "agent" section of housekeeping.json (AGENTS.md Rule 0).
"""
import json
import time
from datetime import datetime
from pathlib import Path

import housekeeping
from ollama_output import call_with_schema

CONFIG = {}


def configure(path=None) -> dict:
    """(Re)load housekeeping.json's "agent" section."""
    global CONFIG
    CONFIG = housekeeping.load(path)["agent"]
    return CONFIG


def _text(key, **fields) -> str:
    return CONFIG["text"][key].format(**fields)


configure()


def run(goal, tools, required, decide, *, active=lambda: True, max_steps=None):
    max_steps = CONFIG["max_steps"] if max_steps is None else max_steps
    note_max, focus_max, prefix = CONFIG["note_max"], CONFIG["focus_max"], CONFIG["observation_prefix"]
    result = {"status": "failed", "steps": [], "evidence": {}, "suggestion": "",
              "focus_ids": [], "error": _text("steps_exhausted")}
    messages = [{"role": "system", "content": _text("system", required=", ".join(sorted(required)),
                                                    note_max=note_max)},
                {"role": "user", "content": _text("goal", goal=goal,
                                                  now=datetime.now().astimezone().isoformat(timespec="minutes"))}]
    deadline = time.monotonic() + CONFIG["run_budget_s"]
    for _ in range(max_steps):
        if not active():
            result.update(status="paused", error=_text("paused"))
            return result
        if time.monotonic() >= deadline:
            result["error"] = _text("time_exhausted")
            return result
        try:
            available = tuple(name for name in tools if name not in result["evidence"])
            if required <= result["evidence"].keys():
                available += ("finish",)
            choice = decide(messages, available)
            if (not isinstance(choice, dict) or set(choice) - {"tool", "note", "focus_ids"}
                    or not isinstance(choice.get("tool"), str)
                    or not isinstance(choice.get("note"), str)
                    or len(choice["note"]) > note_max
                    or not isinstance(choice.get("focus_ids"), list)
                    or len(choice["focus_ids"]) > focus_max
                    or any(not isinstance(i, str) for i in choice["focus_ids"])):
                raise ValueError(_text("invalid_decision"))
            # A pause while the model was running must precede the next action.
            if not active():
                result.update(status="paused", error=_text("paused"))
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError(_text("time_exhausted"))
            result["steps"].append(choice)
            name = choice["tool"]
            if name == "finish":
                observed = {t["id"] for e in result["evidence"].values()
                            for t in e.get("tasks", [])}
                if not required <= result["evidence"].keys():
                    feedback = _text("missing_required")
                elif not set(choice["focus_ids"]) <= observed:
                    feedback = _text("unknown_task")
                elif observed and not choice["focus_ids"]:
                    feedback = _text("no_focus")
                else:
                    result.update(status="completed", suggestion=choice["note"],
                                  focus_ids=choice["focus_ids"], error="")
                    return result
            elif name not in tools:
                raise ValueError(_text("not_permitted", name=name))
            else:
                if name not in result["evidence"]:
                    observation = tools[name]()
                    if not isinstance(observation, dict) or observation.get("error"):
                        raise ValueError(_text("observation_failed", observation=observation))
                    result["evidence"][name] = observation
                    feedback = json.dumps(observation, ensure_ascii=False)
                else:
                    feedback = _text("already_observed")
            messages.extend([{"role": "assistant", "content": json.dumps(choice)},
                             {"role": "user", "content": prefix + feedback}])
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
    return result


class LocalModel:
    """Same local model/context as chat; finite HTTP timeout and output budget."""
    def __call__(self, messages, names):
        import ollama
        from brain import MODEL, NUM_CTX, _chat_extra
        observed_ids = set()
        prefix = CONFIG["observation_prefix"]
        for message in messages:
            content = message.get("content", "")
            if message.get("role") == "user" and content.startswith(prefix + "{"):
                data = json.loads(content[len(prefix):])
                observed_ids.update(t["id"] for t in data.get("tasks", []))
        schema = json.loads(Path(housekeeping.path(CONFIG["schema_file"])).read_text(encoding="utf-8"))
        props = schema["properties"]
        props["tool"]["enum"] = list(names)
        props["note"].update(maxLength=CONFIG["note_max"], description=_text("note_description"))
        props["focus_ids"]["maxItems"] = min(CONFIG["focus_max"], len(observed_ids))
        if observed_ids:
            props["focus_ids"]["items"]["enum"] = sorted(observed_ids)
        # The same schema grounds the prompt when an older server cannot constrain decoding.
        instruction = {"role": "system", "content": json.dumps(schema)}
        if messages and messages[0].get("role") == "system":
            instruction["content"] = messages[0].get("content", "") + "\n\n" + instruction["content"]
            messages = [instruction, *messages[1:]]
        else:
            messages = [instruction, *messages]
        response = call_with_schema(ollama.Client(timeout=CONFIG["model_timeout_s"]).chat,
            schema=schema, enabled=CONFIG["structured_output"], **_chat_extra(),
            model=MODEL, messages=messages, keep_alive=CONFIG["keep_alive"],
            options={"temperature": CONFIG["temperature"], "num_ctx": NUM_CTX,
                     "num_predict": CONFIG["num_predict"]})
        text = response.message.content or ""
        if len(text) > CONFIG["reply_max_chars"]:
            raise ValueError(_text("reply_too_large"))
        return json.loads(text)
