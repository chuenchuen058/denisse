"""Schema-constrained local calls with a narrow, single compatibility retry.

Callers own their schema and opt-out. Prompt examples can supply types without
copying their fields into a second schema; example values never become constants.
"""
from __future__ import annotations

import inspect
import io
import json
import logging
import re
from pathlib import Path
from urllib.error import HTTPError


def schema_from_example(example) -> dict:
    """Infer JSON types/required fields, not values, enums or list lengths."""
    if isinstance(example, dict):
        return {"type": "object", "properties": {
            key: schema_from_example(value) for key, value in example.items()},
            "required": list(example), "additionalProperties": False}
    if isinstance(example, list):
        shapes = []
        for value in example:
            shape = schema_from_example(value)
            if shape not in shapes:
                shapes.append(shape)
        items = shapes[0] if len(shapes) == 1 else {"anyOf": shapes} if shapes else {}
        return {"type": "array", "items": items}
    return {"type": {str: "string", bool: "boolean", int: "integer",
                     float: "number", type(None): "null"}[type(example)]}


def accepts_keyword(call, name: str) -> bool:
    """Keep injected two-argument tutor calls and non-local exam agents working."""
    try:
        params = inspect.signature(call).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.kind is p.VAR_KEYWORD or (p.name == name and p.kind in
               (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)) for p in params)


def _format_rejected(exc: Exception) -> bool:
    policy = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))
    status = exc.code if isinstance(exc, HTTPError) else getattr(exc, "status_code", None)
    if status not in policy["rejection_statuses"]:
        return False
    detail = getattr(exc, "error", str(exc))
    if isinstance(exc, HTTPError):
        # Preserve the error body for the caller if this is not a format error.
        body = exc.read()
        exc.fp = io.BytesIO(body)
        exc.file = exc.fp
        exc.read = exc.fp.read  # HTTPError caches its first delegated read method.
        detail = body.decode("utf-8", errors="replace")
    return bool(re.search(policy["rejection_pattern"], str(detail), re.IGNORECASE))


def call_with_schema(call, *, schema: dict | None, enabled: bool, **kwargs):
    """Keep all arguments (including think) on retry; never retry unrelated errors."""
    if not enabled or schema is None:
        return call(**kwargs)
    try:
        return call(format=schema, **kwargs)
    except Exception as exc:
        if not _format_rejected(exc):
            raise
        logging.warning("Ollama rejected structured output; retrying without format: %s", exc)
        return call(**kwargs)
