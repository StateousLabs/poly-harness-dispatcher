from helpers import feed_all
from phd.harnesses.codex.parser import CodexParser
from phd.results import Usage


def test_codex_ok_ignores_hook_trust_warning():
    events, o = feed_all(CodexParser(), "codex_ok.jsonl")
    assert [t for t, _ in events] == ["message", "tool_call", "tool_result", "tool_call", "tool_result", "message", "usage"]
    assert o.session_id == "01a0f55a-d4f3-7d43-943f-8708f4dfa0cf"
    assert o.result_text == "HELPER-IDENTITY-OK\nphd-echo: ping\nRULES-OK"
    assert o.usage == Usage(input_tokens=55889, cached_input_tokens=50176, output_tokens=260, reasoning_tokens=24)
    assert o.cost_usd is None and o.cost_source == "unknown" and o.num_turns == 1 and o.error is None


def test_codex_model_404_is_model_unavailable():
    _, o = feed_all(CodexParser(), "codex_model_404.jsonl")
    assert o.error.kind == "model_unavailable" and "gpt-5.5" in o.error.message


def test_codex_malformed_item_string():
    """Codex parser should not raise on item being a string."""
    p = CodexParser()
    events = p.feed({"type": "item.completed", "item": "x"})
    # Non-dict items are skipped gracefully, no exception raised
    assert p.outcome() is not None


def test_codex_malformed_item_message_null():
    """Codex parser should not raise on item.message being null."""
    p = CodexParser()
    events = p.feed({"type": "item.completed", "item": {"type": "error", "message": None}})
    # None messages are skipped gracefully, no exception raised
    assert p.outcome() is not None


def test_codex_malformed_token_counts():
    """Codex parser should coerce string token counts without raising."""
    p = CodexParser()
    events = p.feed({"type": "turn.completed", "usage": {"input_tokens": "50", "output_tokens": "10"}})
    assert [t for t, _ in events] == ["usage"]
    assert p.outcome().usage.input_tokens == 50
    assert p.outcome().usage.output_tokens == 10


def test_codex_error_recovery():
    """Codex parser should clear error on successful turn.completed."""
    p = CodexParser()
    p.feed({"type": "error", "message": "something failed"})
    assert p.outcome().error is not None
    p.feed({"type": "turn.completed", "usage": {}})
    assert p.outcome().error is None
