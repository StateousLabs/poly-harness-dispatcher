"""Collector: one parser per harness turns the CLI's JSON stream into normalized events and a
final Outcome (CoreDesign §2e). Event shapes verified against claude 2.1.286, codex 0.147.0 and
opencode 1.18.18 (CoreDesign §14.1)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .result import RunError, Usage


@dataclass
class Outcome:
    session_id: str | None = None
    result_text: str | None = None
    structured_output: Any = None
    cost_usd: float | None = None
    cost_source: str = "unknown"
    usage: Usage = field(default_factory=Usage)
    num_turns: int | None = None
    error: RunError | None = None
    cli_version: str | None = None
    raw_final: dict | None = None


def classify_error(message: str) -> str:
    m = message.lower()
    if "do not have access" in m or ("model" in m and ("does not exist" in m or "not found" in m or "not supported" in m)):
        return "model_unavailable"
    if "insufficient" in m and ("fund" in m or "credit" in m) or "402" in m or "billing" in m:
        return "billing"
    if "not logged in" in m or "unauthorized" in m or "401" in m or "authentication" in m or "invalid api key" in m:
        return "auth"
    if "budget" in m:
        return "budget"
    return "harness_error"


class ClaudeParser:
    def __init__(self) -> None:
        self.out = Outcome()

    def feed(self, obj: dict) -> list[tuple[str, dict]]:
        kind, events = obj.get("type"), []
        if kind == "system" and obj.get("subtype") == "init":
            self.out.session_id = obj.get("session_id")
            self.out.cli_version = obj.get("claude_code_version")
        elif kind == "assistant":
            for item in (obj.get("message") or {}).get("content") or []:
                if item.get("type") == "text" and item.get("text"):
                    events.append(("message", {"text": item["text"]}))
                elif item.get("type") == "tool_use":
                    events.append(("tool_call", {"tool": item.get("name"), "input": item.get("input"), "id": item.get("id")}))
        elif kind == "user":
            for item in (obj.get("message") or {}).get("content") or []:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    events.append(("tool_result", {"id": item.get("tool_use_id"), "is_error": bool(item.get("is_error"))}))
        elif kind == "result":
            o, usage = self.out, obj.get("usage") or {}
            o.session_id = obj.get("session_id") or o.session_id
            o.result_text, o.num_turns, o.raw_final = obj.get("result"), obj.get("num_turns"), obj
            o.cost_usd = obj.get("total_cost_usd")
            o.cost_source = "harness" if o.cost_usd is not None else "unknown"
            cache_read = usage.get("cache_read_input_tokens") or 0
            cache_write = usage.get("cache_creation_input_tokens") or 0
            o.usage = Usage(input_tokens=(usage.get("input_tokens") or 0) + cache_read + cache_write,
                            cached_input_tokens=cache_read, output_tokens=usage.get("output_tokens") or 0,
                            reasoning_tokens=(usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
            if "structured_output" in obj:
                o.structured_output = obj["structured_output"]
            if obj.get("is_error"):
                message = str(obj.get("result") or obj.get("subtype") or "claude reported an error")
                kind_ = "budget" if "budget" in str(obj.get("subtype", "")) else classify_error(message)
                o.error = RunError(kind_, message)
            events.append(("usage", asdict(o.usage) | {"cost_usd": o.cost_usd}))
        return events

    def running_cost(self) -> float | None:
        return self.out.cost_usd

    def outcome(self) -> Outcome:
        return self.out


class CodexParser:
    BENIGN_WARNINGS = ("--dangerously-bypass-hook-trust",)
    TOOL_ITEMS = ("command_execution", "mcp_tool_call", "file_change", "web_search")

    def __init__(self) -> None:
        self.out = Outcome()

    def feed(self, obj: dict) -> list[tuple[str, dict]]:
        kind, item, events = obj.get("type"), obj.get("item") or {}, []
        if kind == "thread.started":
            self.out.session_id = obj.get("thread_id")
        elif kind in ("item.started", "item.completed"):
            item_type = item.get("type")
            if item_type == "agent_message" and kind == "item.completed":
                self.out.result_text = item.get("text")
                events.append(("message", {"text": item.get("text")}))
            elif item_type in self.TOOL_ITEMS:
                tool = f"mcp:{item.get('server')}/{item.get('tool')}" if item_type == "mcp_tool_call" else item_type
                events.append(("tool_call" if kind == "item.started" else "tool_result",
                               {"tool": tool, "id": item.get("id"), "status": item.get("status")}))
            elif item_type == "error" and kind == "item.completed":
                message = item.get("message", "")
                if not any(w in message for w in self.BENIGN_WARNINGS):
                    events.append(("log", {"text": message, "level": "warning"}))
        elif kind == "turn.completed":
            u = obj.get("usage") or {}
            self.out.usage.add(Usage(input_tokens=u.get("input_tokens") or 0,
                                     cached_input_tokens=u.get("cached_input_tokens") or 0,
                                     output_tokens=u.get("output_tokens") or 0,
                                     reasoning_tokens=u.get("reasoning_output_tokens") or 0))
            self.out.num_turns = (self.out.num_turns or 0) + 1
            self.out.raw_final = obj
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


class OpencodeParser:
    def __init__(self) -> None:
        self.out = Outcome(cost_source="harness")
        self._cost = 0.0

    def feed(self, obj: dict) -> list[tuple[str, dict]]:
        kind, part, events = obj.get("type"), obj.get("part") or {}, []
        self.out.session_id = self.out.session_id or obj.get("sessionID")
        if kind == "text" and part.get("text"):
            self.out.result_text = part["text"]
            events.append(("message", {"text": part["text"]}))
        elif kind == "tool_use":
            events.append(("tool_call", {"tool": part.get("tool"), "id": part.get("callID") or part.get("id"),
                                         "status": (part.get("state") or {}).get("status")}))
        elif kind == "step_finish":
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            self.out.usage.add(Usage(
                input_tokens=(tokens.get("input") or 0) + (cache.get("read") or 0) + (cache.get("write") or 0),
                cached_input_tokens=cache.get("read") or 0, output_tokens=tokens.get("output") or 0,
                reasoning_tokens=tokens.get("reasoning") or 0))
            self._cost += part.get("cost") or 0
            self.out.cost_usd, self.out.raw_final = self._cost, obj
            self.out.num_turns = (self.out.num_turns or 0) + 1
            events.append(("usage", asdict(self.out.usage) | {"cost_usd": self._cost}))
        elif kind == "error":
            err = obj.get("error") or {}
            message = (err.get("data") or {}).get("message") or err.get("name") or str(err)
            self.out.error = RunError(classify_error(message), message)
        return events

    def running_cost(self) -> float | None:
        return self._cost

    def outcome(self) -> Outcome:
        return self.out
