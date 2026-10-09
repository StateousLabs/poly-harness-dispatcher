"""Claude stream parser: the CLI's JSON stream → normalized events and a final Outcome
(CoreDesign §2e). Event shapes verified against claude 2.1.286 (CoreDesign §14.1)."""

from __future__ import annotations

from dataclasses import asdict

from ...results import RunError, Usage
from ..outcome import Outcome, as_int, classify_error
from ..summary import outcome_line, tool_line

# tool name -> the input key that says what the call acts on
_KEY_ARGUMENT = {"Bash": "command", "Read": "file_path", "Edit": "file_path", "Write": "file_path",
                 "MultiEdit": "file_path", "NotebookEdit": "notebook_path", "Glob": "pattern", "Grep": "pattern",
                 "WebFetch": "url", "WebSearch": "query", "Task": "description"}


class ClaudeParser:
    def __init__(self) -> None:
        self.out = Outcome()
        self._tool_names: dict[str, str] = {}  # tool_use id -> tool name, to name a tool_result

    def summarize(self, kind: str, data: dict) -> str:
        """Raw (unformatted) `Event.summary` text for an event this parser produced."""
        if kind == "message":
            return str(data.get("text") or "")
        if kind == "tool_call":
            name, args = str(data.get("tool") or "tool"), data.get("input")
            key = _KEY_ARGUMENT.get(name)
            return tool_line(name, args.get(key) if key and isinstance(args, dict) else None)
        if kind == "tool_result":
            return outcome_line(self._tool_names.pop(data.get("id"), "tool"), not data.get("is_error"))
        return ""

    def feed(self, obj: dict) -> list[tuple[str, dict]]:
        try:
            return self._feed(obj)
        except (AttributeError, TypeError, ValueError, KeyError):
            return [("log", {"text": "unparsed claude event", "raw_type": str(obj.get("type"))})]

    def _feed(self, obj: dict) -> list[tuple[str, dict]]:
        kind, events = obj.get("type"), []
        if kind == "system" and obj.get("subtype") == "init":
            self.out.session_id = obj.get("session_id")
            self.out.cli_version = obj.get("claude_code_version")
        elif kind == "assistant":
            for item in (obj.get("message") or {}).get("content") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and item.get("text"):
                    events.append(("message", {"text": item["text"]}))
                elif item.get("type") == "tool_use":
                    events.append(("tool_call", {"tool": item.get("name"), "input": item.get("input"), "id": item.get("id")}))
                    self._tool_names[item.get("id")] = str(item.get("name") or "tool")
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
            cache_read = as_int(usage.get("cache_read_input_tokens"))
            cache_write = as_int(usage.get("cache_creation_input_tokens"))
            o.usage = Usage(input_tokens=as_int(usage.get("input_tokens")) + cache_read + cache_write,
                            cached_input_tokens=cache_read, output_tokens=as_int(usage.get("output_tokens")),
                            reasoning_tokens=as_int((usage.get("output_tokens_details") or {}).get("thinking_tokens")))
            if "structured_output" in obj:
                o.structured_output = obj["structured_output"]
            subtype = str(obj.get("subtype") or "")
            if obj.get("is_error") or subtype.startswith("error"):
                errors = ("; ".join(str(e) for e in obj.get("errors") or [])
                          if subtype == "error_during_execution" else "")
                message = str(obj.get("result") or errors or obj.get("subtype") or "claude reported an error")
                kind_ = "budget" if "budget" in subtype else classify_error(message)
                o.error = RunError(kind_, message)
            events.append(("usage", asdict(o.usage) | {"cost_usd": o.cost_usd}))
        return events

    def running_cost(self) -> float | None:
        return self.out.cost_usd

    def outcome(self) -> Outcome:
        return self.out
