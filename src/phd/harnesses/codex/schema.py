"""Codex `--output-schema` translation: OpenAI strict-mode conversion and the inverse on the answer.

Strict mode wants an object root, `anyOf` (not `oneOf`), every property listed in `required`, and
`additionalProperties: false` on every object. `to_strict` converts the caller's schema (wrapping a non-object
root); `restore` turns the model's answer back into the caller's shape (unwrap, drop `null`s of optional
properties). PHD then validates the restored answer against the caller's original schema."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ...errors import UnsupportedFeature
from ..schema_wrap import needs_wrap, unwrap, wrap

# Keywords the conversion cannot express; refused rather than silently dropped.
UNSUPPORTED = ("patternProperties", "if", "then", "else", "not", "dependentSchemas", "dependentRequired",
               "unevaluatedProperties", "unevaluatedItems")
_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_MAPS = ("$defs", "definitions")
_NULL = {"type": "null"}


def to_strict(schema: dict) -> dict:
    """The strict-mode form of `schema` (a copy; the caller's is untouched).

    Raises:
        UnsupportedFeature: `schema` uses a keyword in `UNSUPPORTED`."""
    return _strict(wrap(schema) if needs_wrap(schema) else deepcopy(schema), "$")


def _strict(node: Any, path: str) -> Any:
    if not isinstance(node, dict):
        return node
    for key in UNSUPPORTED:
        if key in node:
            raise UnsupportedFeature(f"codex cannot express the {key!r} keyword in output_schema (at {path}); "
                                     "use a simpler schema or another harness")
    out = dict(node)
    if "oneOf" in out:
        branches = out.pop("oneOf")
        if "anyOf" in out:  # both present: keep both constraints
            out["allOf"] = [*out.get("allOf", []), {"anyOf": branches}]
        else:
            out["anyOf"] = branches
    for key in _LISTS:
        if isinstance(out.get(key), list):
            out[key] = [_strict(s, f"{path}/{key}/{i}") for i, s in enumerate(out[key])]
    for key in _MAPS:
        if isinstance(out.get(key), dict):
            out[key] = {k: _strict(s, f"{path}/{key}/{k}") for k, s in out[key].items()}
    if isinstance(out.get("items"), dict):
        out["items"] = _strict(out["items"], f"{path}/items")
    if out.get("type") == "object" or "properties" in out:
        required = list(out.get("required") or [])
        props = out.get("properties") or {}
        new = {}
        for name, sub in props.items():
            sub = _strict(sub, f"{path}/properties/{name}")
            new[name] = sub if name in required else {"anyOf": [sub, dict(_NULL)]}
        if "properties" in out:
            out["properties"] = new
        out["required"] = required + [n for n in props if n not in required] if props else required
        if not props and not out["required"]:
            out.pop("required")
        out["additionalProperties"] = False
    return out


def restore(schema: dict, value: Any) -> Any:
    """`value` (a Codex answer to `to_strict(schema)`) in the shape `schema` describes."""
    if needs_wrap(schema):
        value = unwrap(value)
    return _strip(schema, [schema], value, 0)


def _resolve(root: dict, node: Any, depth: int = 0) -> Any:
    """Follow a local `$ref` (`#/a/b`); anything else is returned as is."""
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and node["$ref"].startswith("#") and depth < 20:
        target: Any = root
        for part in node["$ref"][1:].split("/"):
            if not part:
                continue
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part) if isinstance(target, dict) else None
        if not isinstance(target, dict):
            return node
        node, depth = target, depth + 1
    return node


def _branches(root: dict, node: Any, depth: int = 0) -> list[dict]:
    """`node` and every schema reachable through `anyOf` / `oneOf` / `allOf` / local `$ref`."""
    node = _resolve(root, node)
    if not isinstance(node, dict) or depth > 20:
        return []
    found = [node]
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in node.get(key) or []:
            found += _branches(root, sub, depth + 1)
    return found


def _allows_null(root: dict, node: Any) -> bool:
    for b in _branches(root, node):
        t = b.get("type")
        if t == "null" or (isinstance(t, list) and "null" in t) or ("const" in b and b["const"] is None) \
                or (isinstance(b.get("enum"), list) and None in b["enum"]):
            return True
    return not _branches(root, node)  # unknown schema: leave the null alone


def _matches_consts(branch: dict, value: dict) -> bool:
    for name, sub in (branch.get("properties") or {}).items():
        if isinstance(sub, dict) and name in value:
            if "const" in sub and sub["const"] != value[name]:
                return False
            if isinstance(sub.get("enum"), list) and value[name] not in sub["enum"]:
                return False
    return True


def _strip(root: dict, nodes: list, value: Any, depth: int) -> Any:
    if depth > 50:
        return value
    candidates = [b for n in nodes for b in _branches(root, n)]
    if isinstance(value, list):
        items = [b["items"] for b in candidates if isinstance(b.get("items"), dict)]
        return [_strip(root, items, v, depth + 1) for v in value]
    if not isinstance(value, dict):
        return value
    objects = [b for b in candidates if "properties" in b or b.get("type") == "object"]
    chosen = [b for b in objects if _matches_consts(b, value)] or objects
    out = {}
    for key, child in value.items():
        subs = [b["properties"][key] for b in chosen if key in (b.get("properties") or {})]
        if child is None and subs and any(key not in (b.get("required") or []) for b in chosen
                                         if key in (b.get("properties") or {})) \
                and not any(_allows_null(root, s) for s in subs):
            continue  # an optional property Codex had to fill with null
        out[key] = _strip(root, subs, child, depth + 1)
    return out
