"""Codex stream parser: the CLI's JSON stream → normalized events and a final Outcome
(CoreDesign §2e). Event shapes verified against codex 0.147.0 (CoreDesign §14.1)."""

from __future__ import annotations

from dataclasses import asdict

from ...results import RunError, Usage
from ..outcome import Outcome, as_int, classify_error
from ..summary import outcome_line, tool_line

_TOOL_NAMES = {"command_execution": "Shell", "file_change": "Edit", "web_search": "WebSearch"}


class CodexParser:
    BENIGN_WARNINGS = ("--dangerously-bypass-hook-trust",)
    TOOL_ITEMS = ("command_execution", "mcp_tool_call", "file_change", "web_search")

    def __init__(self) -> None:
        self.out = Outcome()

    def summarize(self, kind: str, data: dict) -> str:
        """Raw (unformatted) `Event.summary` text for an event this parser produced."""
        if kind == "message":
            return str(data.get("text") or "")
        if kind not in ("tool_call", "tool_result"):
            return ""
        raw_tool = str(data.get("tool") or "")
        name = _TOOL_NAMES.get(raw_tool, raw_tool or "tool")
        if kind == "tool_result":
            ok = data.get("status") == "completed" and data.get("exit_code") in (0, None)
            return outcome_line(name, ok)
        arg = data.get("input")
        if raw_tool == "command_execution":
            return tool_line(name, arg.get("command") if isinstance(arg, dict) else None)
        if raw_tool == "file_change":
            changes = arg.get("changes") if isinstance(arg, dict) else None
            first = changes[0] if isinstance(changes, list) and changes else None
            return tool_line(name, first.get("path") if isinstance(first, dict) else None)
        if raw_tool == "web_search":
            return tool_line(name, arg.get("query") if isinstance(arg, dict) else None)
        return name  # mcp:<server>/<tool>

    def feed(self, obj: dict) -> list[tuple[str, dict]]:
        try:
            return self._feed(obj)
        except (AttributeError, TypeError, ValueError, KeyError):
            return [("log", {"text": "unparsed codex event", "raw_type": str(obj.get("type"))})]

    def _feed(self, obj: dict) -> list[tuple[str, dict]]:
        kind, item, events = obj.get("type"), obj.get("item") or {}, []
        if kind == "thread.started":
            self.out.session_id = obj.get("thread_id")
        elif kind in ("item.started", "item.completed"):
            if not isinstance(item, dict):
                return events
            item_type = item.get("type")
            if item_type == "agent_message" and kind == "item.completed":
                self.out.result_text = item.get("text")
                events.append(("message", {"text": item.get("text")}))
            elif item_type in self.TOOL_ITEMS:
                tool = f"mcp:{item.get('server')}/{item.get('tool')}" if item_type == "mcp_tool_call" else item_type
                data = {"tool": tool, "id": item.get("id"), "status": item.get("status")}
                if kind == "item.started":
                    events.append(("tool_call", data | {"input": self._tool_input(item)}))
                else:
                    events.append(("tool_result", data | {"exit_code": item.get("exit_code")}))
            elif item_type == "error" and kind == "item.completed":
                message = item.get("message") or ""
                if message and not any(w in message for w in self.BENIGN_WARNINGS):
                    events.append(("log", {"text": message, "level": "warning"}))
        elif kind == "turn.completed":
            u = obj.get("usage") or {}
            self.out.usage.add(Usage(input_tokens=as_int(u.get("input_tokens")),
                                     cached_input_tokens=as_int(u.get("cached_input_tokens")),
                                     output_tokens=as_int(u.get("output_tokens")),
                                     reasoning_tokens=as_int(u.get("reasoning_output_tokens"))))
            self.out.num_turns = (self.out.num_turns or 0) + 1
            self.out.raw_final = obj
            self.out.error = None  # clear error on successful turn completion (recovered run)
            events.append(("usage", asdict(self.out.usage) | {"cost_usd": None}))
        elif kind in ("turn.failed", "error"):
            err = obj.get("error")
            message = (err.get("message") if isinstance(err, dict) else None) or obj.get("message") or str(obj)
            self.out.error = RunError(classify_error(message), message)
        return events

    @staticmethod
    def _tool_input(item: dict) -> dict:
        """What the call acts on (never the output): the command, the changed files, the search query."""
        kind = item.get("type")
        if kind == "command_execution":
            return {"command": item.get("command")}
        if kind == "file_change":
            return {"changes": item.get("changes")}
        if kind == "web_search":
            return {"query": item.get("query")}
        return {"arguments": item.get("arguments")}

    def running_cost(self) -> float | None:
        return None  # codex reports no cost

    def outcome(self) -> Outcome:
        return self.out
