import pytest

from helpers import feed_all
from phd.harnesses.claude.parser import ClaudeParser
from phd.results import RunError, Usage


def test_claude_ok():
    events, o = feed_all(ClaudeParser(), "claude_ok.jsonl")
    assert [t for t, _ in events] == ["message", "tool_call", "tool_result", "usage"]
    assert events[1][1] == {"tool": "Bash", "input": {"command": "echo hello-from-shell"}, "id": "tu_1"}
    assert o.session_id == "sess-claude-1" and o.cli_version == "2.1.286"
    assert o.result_text.startswith("HELPER-IDENTITY-OK") and o.num_turns == 3
    assert o.cost_usd == pytest.approx(0.0284) and o.cost_source == "harness"
    assert o.usage == Usage(input_tokens=27 + 8533 + 69875, cached_input_tokens=69875, output_tokens=870, reasoning_tokens=413)
    assert o.error is None and o.raw_final["type"] == "result"


def test_claude_not_logged_in_is_auth():
    _, o = feed_all(ClaudeParser(), "claude_not_logged_in.jsonl")
    assert o.error == RunError("auth", "Not logged in · Please run /login")


def test_claude_malformed_message_string():
    """Claude parser should not raise on message being a string instead of dict."""
    p = ClaudeParser()
    events = p.feed({"type": "assistant", "message": "x"})
    assert [t for t, _ in events] == ["log"]
    assert p.outcome() is not None


def test_claude_malformed_content_item():
    """Claude parser should not raise on content items being strings."""
    p = ClaudeParser()
    events = p.feed({"type": "assistant", "message": {"content": ["str"]}})
    # String items are skipped gracefully, no exception raised
    assert p.outcome() is not None


def test_claude_malformed_token_counts():
    """Claude parser should coerce string token counts without raising."""
    p = ClaudeParser()
    events = p.feed({
        "type": "result",
        "result": "ok",
        "num_turns": 1,
        "total_cost_usd": 0,
        "usage": {"input_tokens": "12", "output_tokens": "5", "cache_read_input_tokens": "100"}
    })
    assert [t for t, _ in events] == ["usage"]
    assert p.outcome().usage.input_tokens == 112  # "12" + "100" (cache_read)
    assert p.outcome().usage.output_tokens == 5


def test_claude_error_subtype_without_is_error_flag():
    p = ClaudeParser()
    p.feed({"type": "result", "subtype": "error_during_execution", "is_error": False, "session_id": "s"})
    assert p.outcome().error == RunError("harness_error", "error_during_execution")
    p = ClaudeParser()
    p.feed({"type": "result", "subtype": "error_max_budget_usd", "is_error": False})
    assert p.outcome().error.kind == "budget"
    p = ClaudeParser()
    p.feed({"type": "result", "subtype": "error_during_execution", "is_error": False, "result": "401 unauthorized"})
    assert p.outcome().error.kind == "auth"
    p = ClaudeParser()
    p.feed({"type": "result", "subtype": "success", "is_error": False, "result": "fine"})
    assert p.outcome().error is None
