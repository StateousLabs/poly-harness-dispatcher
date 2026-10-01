"""OpenCode stream parser: the CLI's JSON stream → normalized events and a final Outcome
(CoreDesign §2e). Event shapes verified against opencode 1.18.18 (CoreDesign §14.1)."""

from __future__ import annotations

from dataclasses import asdict

from ...results import RunError, Usage
from ..outcome import Outcome, as_int, classify_error


class OpencodeParser:
    def __init__(self) -> None:
        self.out = Outcome(cost_source="harness")
        self._cost = 0.0

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
            events.append(("tool_call", {"tool": part.get("tool"), "id": part.get("callID") or part.get("id"),
                                         "status": (part.get("state") or {}).get("status")}))
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
