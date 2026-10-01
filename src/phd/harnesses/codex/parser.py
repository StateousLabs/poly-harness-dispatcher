"""Codex stream parser: the CLI's JSON stream → normalized events and a final Outcome
(CoreDesign §2e, event shapes verified in §14.1)."""

from __future__ import annotations

from dataclasses import asdict

from ...results.model import RunError, Usage
from ..outcome import Outcome, _int, classify_error


class CodexParser:
    BENIGN_WARNINGS = ("--dangerously-bypass-hook-trust",)
    TOOL_ITEMS = ("command_execution", "mcp_tool_call", "file_change", "web_search")

    def __init__(self) -> None:
        self.out = Outcome()

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
                events.append(("tool_call" if kind == "item.started" else "tool_result",
                               {"tool": tool, "id": item.get("id"), "status": item.get("status")}))
            elif item_type == "error" and kind == "item.completed":
                message = item.get("message") or ""
                if message and not any(w in message for w in self.BENIGN_WARNINGS):
                    events.append(("log", {"text": message, "level": "warning"}))
        elif kind == "turn.completed":
            u = obj.get("usage") or {}
            self.out.usage.add(Usage(input_tokens=_int(u.get("input_tokens")),
                                     cached_input_tokens=_int(u.get("cached_input_tokens")),
                                     output_tokens=_int(u.get("output_tokens")),
                                     reasoning_tokens=_int(u.get("reasoning_output_tokens"))))
            self.out.num_turns = (self.out.num_turns or 0) + 1
            self.out.raw_final = obj
            self.out.error = None  # clear error on successful turn completion (recovered run)
            events.append(("usage", asdict(self.out.usage) | {"cost_usd": None}))
        elif kind in ("turn.failed", "error"):
            err = obj.get("error")
            message = (err.get("message") if isinstance(err, dict) else None) or obj.get("message") or str(obj)
            self.out.error = RunError(classify_error(message), message)
        return events

    def running_cost(self) -> float | None:
        return None  # codex reports no cost

    def outcome(self) -> Outcome:
        return self.out
