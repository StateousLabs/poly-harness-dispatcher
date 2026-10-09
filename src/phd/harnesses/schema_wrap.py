"""Wrap a non-object `output_schema` in an object, for harnesses whose structured-output flag needs an object root.

Claude (`--json-schema`) and Codex (`--output-schema`) refuse a root `oneOf` / `anyOf` / `$ref` / non-object schema.
The caller's schema then goes under the single property `outcome`, and `unwrap` takes it back out."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

WRAPPER_KEY = "outcome"
_COMPOSITES = ("oneOf", "anyOf", "allOf", "$ref")
_ROOT_KEYS = ("$schema", "$defs", "definitions")  # stay at the wrapper root so `#/$defs/...` refs keep resolving


def needs_wrap(schema: dict) -> bool:
    """`True` unless the root is already an object schema (`type: "object"`, no root `oneOf`/`anyOf`/`allOf`/`$ref`)."""
    return schema.get("type") != "object" or any(k in schema for k in _COMPOSITES)


def wrap(schema: dict) -> dict:
    """`schema` as the `outcome` property of an object schema (`$schema`, `$defs`, `definitions` hoisted to the root)."""
    inner = deepcopy(schema)
    hoisted = {k: inner.pop(k) for k in _ROOT_KEYS if k in inner}
    return {**hoisted, "type": "object", "properties": {WRAPPER_KEY: inner}, "required": [WRAPPER_KEY],
            "additionalProperties": False}


def unwrap(value: Any) -> Any:
    """The `outcome` of a wrapped answer; anything else unchanged (so validation reports it)."""
    if isinstance(value, dict) and WRAPPER_KEY in value:
        return value[WRAPPER_KEY]
    return value
