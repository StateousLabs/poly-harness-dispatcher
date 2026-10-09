"""Shared parser plumbing: the normalized Outcome and error classification (CoreDesign §2e)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..results import RunError, Usage


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


def as_int(v: Any) -> int:
    """Coerce token counts safely; return 0 for non-numeric values."""
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            return 0
    return 0


# What each CLI prints when asked to resume a session it does not have (captured from the real CLIs):
#   claude   result line, errors: ["No conversation found with session ID: <id>"]
#   codex    stderr, exit 1: "Error: thread/resume: thread/resume failed: no rollout found for thread id <id> (code -32600)"
#   opencode stderr, exit 1: "Error: Session not found" (whole line, with ANSI colour codes)
# Anchored to those messages: a bare "session not found" is also what an MCP server returns for an expired
# Mcp-Session-Id, which must never be read as "the harness session is gone".
_ANSI = re.compile(chr(27) + r"\[[0-9;]*m")
_SESSION_GONE_TEXT = ("no conversation found with session id", "thread/resume failed: no rollout found for thread id")


def _session_gone(message: str) -> bool:
    m = message.lower()
    if any(text in m for text in _SESSION_GONE_TEXT):
        return True
    return any(_ANSI.sub("", line).strip() == "error: session not found" for line in m.splitlines())


def classify_error(message: str) -> str:
    m = message.lower()
    if _session_gone(message):  # the harness no longer has the session we asked it to resume
        return "session"
    if "do not have access" in m or ("model" in m and ("does not exist" in m or "not found" in m or "not supported" in m)):
        return "model_unavailable"
    if ("insufficient" in m and ("fund" in m or "credit" in m)) or re.search(r"\b402\b", m) or "billing" in m:
        return "billing"
    if "not logged in" in m or "unauthorized" in m or re.search(r"\b401\b", m) or "authentication" in m or "invalid api key" in m:
        return "auth"
    if "budget" in m:
        return "budget"
    return "harness_error"
