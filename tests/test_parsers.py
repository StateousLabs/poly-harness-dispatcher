import json

import pytest

from conftest import STREAMS
from phd.parsers import ClaudeParser, CodexParser, OpencodeParser, classify_error
from phd.results import RunError, RunResult, RunStatus, Usage
from phd.results.model import decide_status


def feed_all(parser, name):
    events = []
    for line in (STREAMS / name).read_text(encoding="utf-8").splitlines():
        events += parser.feed(json.loads(line))
    return events, parser.outcome()


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


def test_opencode_ok_sums_steps():
    events, o = feed_all(OpencodeParser(), "opencode_ok.jsonl")
    assert [t for t, _ in events] == ["tool_call", "usage", "message", "usage"]
    assert o.session_id == "ses_abc" and o.result_text.endswith("RULES-OK")
    assert o.usage == Usage(input_tokens=5800 + 8523 + 1000, cached_input_tokens=1000, output_tokens=350, reasoning_tokens=100)
    assert o.cost_usd == pytest.approx(0.003) and o.cost_source == "harness"


def test_opencode_402_is_billing():
    _, o = feed_all(OpencodeParser(), "opencode_402.jsonl")
    assert o.error == RunError("billing", "Upstream request failed: Insufficient account funds")


def test_running_cost():
    p = OpencodeParser()
    p.feed(json.loads((STREAMS / "opencode_costly.jsonl").read_text(encoding="utf-8")))
    assert p.running_cost() == pytest.approx(0.5)
    assert CodexParser().running_cost() is None


@pytest.mark.parametrize("message, kind", [
    ("The model `x` does not exist or you do not have access to it.", "model_unavailable"),
    ("Insufficient account funds", "billing"),
    ("401 Unauthorized", "auth"),
    ("Exceeded max budget", "budget"),
    ("something else broke", "harness_error"),
])
def test_classify_error(message, kind):
    assert classify_error(message) == kind


def test_decide_status():
    assert decide_status(None) is RunStatus.SUCCEEDED
    assert decide_status(RunError("auth", "x")) is RunStatus.AUTH_FAILED
    assert decide_status(RunError("timeout", "x")) is RunStatus.TIMED_OUT
    assert decide_status(RunError("budget", "x")) is RunStatus.BUDGET_EXCEEDED
    assert decide_status(RunError("cancelled", "x")) is RunStatus.CANCELLED
    assert decide_status(RunError("billing", "x")) is RunStatus.FAILED


def test_run_result_round_trip():
    r = RunResult(run_id="r1", harness="claude", model="haiku", stack="s@1", stack_hash="sha256:x",
                  status=RunStatus.FAILED, exit_code=1, error=RunError("auth", "no"), result_text=None,
                  structured_output={"a": 1}, cost_usd=None, cost_source="unknown",
                  usage=Usage(1, 0, 2, 0), num_turns=None, duration_ms=5, session_id=None,
                  resumable=False, resumed_from=None, run_dir="/r", home_dir="/r/home", cwd="/w",
                  project_config="ignore", autonomy="restricted", unsafe=False,
                  credential_kind="claude_oauth_token", started_at="2026-10-01T00:00:00+00:00",
                  finished_at="2026-10-01T00:00:01+00:00")
    d = json.loads(json.dumps(r.to_dict()))
    assert d["status"] == "failed"
    assert RunResult.from_dict(d) == r


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


@pytest.mark.parametrize("message, kind", [
    ("request 14012 failed", "harness_error"),
    ("HTTP 402 Payment Required", "billing"),
    ("status 401 unauthorized", "auth"),
])
def test_classify_error_with_boundaries(message, kind):
    """classify_error should use word boundaries for error codes."""
    assert classify_error(message) == kind


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
