"""Shared formatting for `Event.summary`: one plain line, at most `MAX_SUMMARY` characters.

Each harness package turns an event into raw summary text (`<Tool>: <key argument>`, `<Tool>: ok`, or the
agent's text); `finish` applies the rules every harness shares."""

from __future__ import annotations

MAX_SUMMARY = 500
SUMMARISED = ("message", "tool_call", "tool_result")


def tool_line(tool: object, argument: object = None) -> str:
    """`<tool>: <argument>`, or just the tool name when there is no usable argument."""
    name = str(tool or "tool")
    arg = argument if isinstance(argument, str) else ""
    return f"{name}: {arg}" if arg.strip() else name


def outcome_line(tool: object, ok: bool) -> str:
    """`<tool>: ok` or `<tool>: failed` (never the tool's output)."""
    return f"{tool or 'tool'}: {'ok' if ok else 'failed'}"


def finish(kind: str, text: str) -> str:
    """Format raw summary text for an event of type `kind`.

    Only `message`, `tool_call` and `tool_result` get a summary (else `""`). A message keeps its first non-empty
    line; whitespace runs collapse to one space; text over `MAX_SUMMARY` is cut to 499 characters plus `…`."""
    if kind not in SUMMARISED:
        return ""
    if kind == "message":
        text = next((line for line in str(text or "").splitlines() if line.strip()), "")
    line = " ".join(str(text or "").split())
    return line if len(line) <= MAX_SUMMARY else line[:MAX_SUMMARY - 1] + "…"
