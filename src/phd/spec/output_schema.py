"""Structured answers: check an `output_schema`, pull a JSON answer out of text, validate it.

The only module that imports `jsonschema`. The draft comes from the schema's `$schema`
(default 2020-12)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import jsonschema
from jsonschema.exceptions import best_match
from jsonschema.validators import validator_for

MAX_MESSAGE = 500
_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n?```", re.DOTALL)


def _validator(schema: dict):
    return validator_for(schema, default=jsonschema.Draft202012Validator)


def _cap(message: str) -> str:
    return message if len(message) <= MAX_MESSAGE else message[:MAX_MESSAGE - 1] + "…"


def schema_error(schema: dict) -> str | None:
    """`None` when `schema` is a valid JSON Schema, else a short description of the problem."""
    try:
        _validator(schema).check_schema(schema)
    except jsonschema.SchemaError as e:
        return _cap(f"{e.message} (at {e.json_path})")
    return None


def extract_json(text: str) -> Any:
    """The JSON value in `text`: the whole text, or the content of its single fenced block."""
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    blocks = _FENCE.findall(stripped)
    if len(blocks) == 1:
        try:
            return json.loads(blocks[0])
        except json.JSONDecodeError:
            pass
    raise ValueError("final answer is not JSON")


def validate_answer(schema: dict, value: Any, scrub: Callable[[str], str] | None = None) -> str | None:
    """`None` when `value` matches `schema`, else `"<json path>: <message>"` (scrubbed, then capped)."""
    error = best_match(_validator(schema)(schema).iter_errors(value))
    if error is None:
        return None
    message = f"{error.json_path}: {error.message}"
    return _cap(scrub(message) if scrub else message)


def answer_instruction(schema: dict) -> str:
    """System-prompt text asking for a final answer that matches `schema` (harnesses without native support)."""
    return ("\n\n## Final answer format\n\nYour final message must be only a JSON value that matches this "
            "JSON Schema, with no other text:\n\n```json\n" + json.dumps(schema, indent=2) + "\n```\n")
