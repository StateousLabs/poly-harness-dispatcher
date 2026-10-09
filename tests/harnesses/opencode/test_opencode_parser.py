import pytest

from helpers import feed_all
from phd.harnesses.opencode.parser import OpencodeParser
from phd.results import RunError, Usage


def test_opencode_ok_sums_steps():
    events, o = feed_all(OpencodeParser(), "opencode_ok.jsonl")
    assert [t for t, _ in events] == ["tool_call", "tool_result", "usage", "message", "usage"]
    assert o.session_id == "ses_abc" and o.result_text.endswith("RULES-OK")
    assert o.usage == Usage(input_tokens=5800 + 8523 + 1000, cached_input_tokens=1000, output_tokens=350, reasoning_tokens=100)
    assert o.cost_usd == pytest.approx(0.003) and o.cost_source == "harness"


def test_opencode_402_is_billing():
    _, o = feed_all(OpencodeParser(), "opencode_402.jsonl")
    assert o.error == RunError("billing", "Upstream request failed: Insufficient account funds")


def test_opencode_malformed_error_string():
    """OpenCode parser should not raise on error being a plain string."""
    p = OpencodeParser()
    events = p.feed({"type": "error", "error": "boom"})
    assert events == []
    assert p.outcome().error == RunError("harness_error", "boom")
    p = OpencodeParser()
    p.feed({"type": "error", "error": "401 unauthorized"})
    assert p.outcome().error.kind == "auth"


def test_opencode_malformed_token_counts():
    """OpenCode parser should coerce string token counts without raising."""
    p = OpencodeParser()
    events = p.feed({
        "type": "step_finish",
        "part": {
            "tokens": {"input": "5", "output": "10", "cache": {"read": "2"}}
        }
    })
    assert [t for t, _ in events] == ["usage"]
    assert p.outcome().usage.input_tokens == 7  # "5" + "2"
    assert p.outcome().usage.output_tokens == 10


def test_opencode_isretryable_error_as_warning():
    """OpenCode parser should treat isRetryable:true errors as warnings, not failures."""
    p = OpencodeParser()
    events = p.feed({
        "type": "error",
        "error": {"data": {"message": "Temporary issue", "isRetryable": True}}
    })
    assert [t for t, _ in events] == ["log"]
    assert events[0][1]["level"] == "warning"
    assert p.outcome().error is None


def test_opencode_non_retryable_error():
    """OpenCode parser should treat isRetryable:false errors as actual failures."""
    p = OpencodeParser()
    events = p.feed({
        "type": "error",
        "error": {"data": {"message": "Permanent failure", "isRetryable": False}}
    })
    assert p.outcome().error is not None
