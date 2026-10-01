"""Minimal TOML writer for the config files PHD generates (codex config.toml).

Supports scalars, arrays, inline tables inside arrays, nested tables and arrays of tables —
everything PHD writes, nothing more."""

import json
import re
from typing import Any

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def dumps(data: dict[str, Any]) -> str:
    lines: list[str] = []
    _table(lines, [], data)
    return "\n".join(lines).strip() + "\n"


def _key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _dotted(path: list[str]) -> str:
    return ".".join(_key(p) for p in path)


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # JSON escapes are valid TOML basic-string escapes
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def _table(lines: list[str], path: list[str], table: dict[str, Any]) -> None:
    if path:
        lines += ["", f"[{_dotted(path)}]"]
    for key, value in table.items():
        if not isinstance(value, dict) and not _is_table_array(value):
            lines.append(f"{_key(key)} = {_value(value)}")
    for key, value in table.items():
        if isinstance(value, dict):
            _table(lines, [*path, key], value)
    for key, value in table.items():
        if _is_table_array(value):
            for item in value:
                lines += ["", f"[[{_dotted([*path, key])}]]"]
                for item_key, item_value in item.items():
                    lines.append(f"{_key(item_key)} = {_value(item_value)}")
