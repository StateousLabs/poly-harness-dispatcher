"""OpenCode stream parser: the CLI's JSON stream → normalized events and a final Outcome
(CoreDesign §2e). Event shapes verified against opencode 1.18.18 (CoreDesign §14.1)."""

from __future__ import annotations

from dataclasses import asdict

from ...results import RunError, Usage
from ..outcome import Outcome, as_int, classify_error
from ..summary import outcome_line, tool_line

# lower-case tool name -> the input key that says what the call acts on
_KEY_ARGUMENT = {"bash": "command", "read": "filePath", "edit": "filePath", "write": "filePath", "glob": "pattern",
                 "grep": "pattern", "webfetch": "url"}


class OpencodeParser:
    def __init__(self) -> None:
        self.out = Outcome(cost_source="harness")
        self._cost = 0.0

    def summarize(self, kind: str, data: dict) -> str:
        """Raw (unformatted) `Event.summary` text for an event this parser produced."""
        if kind == "message":
            return str(data.get("text") or "")
        if kind not in ("tool_call", "tool_result"):
            return ""
        tool = str(data.get("tool") or "tool")
        name = tool[:1].upper() + tool[1:]
        if kind == "tool_result":
            return outcome_line(name, data.get("status") == "completed")
        args, key = data.get("input"), _KEY_ARGUMENT.get(tool.lower())
        return tool_line(name, args.get(key) if key and isinstance(args, dict) else None)

    def feed(self, obj: dict) -> list[tuple[str, dict]]:
        try:
            return self._feed(obj)
        except (AttributeError, TypeError, ValueError, KeyError):
            return [("log", {"text": "unparsed opencode event", "raw_type": str(obj.get("type"))})]

    def _feed(self, obj: dict) -> list[tuple[str, dict]]:
        kind, part, events = obj.get("type"), obj.get("part") or {}, []
        self.out.session_id = self.out.session_id or obj.get("sessionID")
        if kind == "text" and part.get("text"):
            self.out.result_text = part["text"]
            events.append(("message", {"text": part["text"]}))
        elif kind == "tool_use":
            # OpenCode reports a tool once, when finished: emit the call, then its result
            state = part.get("state") or {}
            data = {"tool": part.get("tool"), "id": part.get("callID") or part.get("id"), "status": state.get("status")}
            events.append(("tool_call", data | {"input": state.get("input")}))
            if state.get("status") in ("completed", "error"):
                events.append(("tool_result", data))
        elif kind == "step_finish":
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            self.out.usage.add(Usage(
                input_tokens=as_int(tokens.get("input")) + as_int(cache.get("read")) + as_int(cache.get("write")),
                cached_input_tokens=as_int(cache.get("read")), output_tokens=as_int(tokens.get("output")),
                reasoning_tokens=as_int(tokens.get("reasoning"))))
            self._cost += part.get("cost") or 0
            self.out.cost_usd, self.out.raw_final = self._cost, obj
            self.out.num_turns = (self.out.num_turns or 0) + 1
            events.append(("usage", asdict(self.out.usage) | {"cost_usd": self._cost}))
        elif kind == "error":
            err = obj.get("error") or {}
            if isinstance(err, str):
                self.out.error = RunError(classify_error(err), err)
                return events
            message = (err.get("data") or {}).get("message") or err.get("name") or str(err)
            is_retryable = (err.get("data") or {}).get("isRetryable") if isinstance(err, dict) else False
            if is_retryable:
                events.append(("log", {"text": message, "level": "warning"}))
            else:
                self.out.error = RunError(classify_error(message), message)
        return events

    def running_cost(self) -> float | None:
        return self._cost

    def outcome(self) -> Outcome:
        return self.out
