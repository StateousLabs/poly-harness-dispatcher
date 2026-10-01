import json

import pytest

from conftest import STREAMS
from phd.parsers import ClaudeParser, CodexParser, OpencodeParser, classify_error
from phd.result import RunError, RunResult, RunStatus, Usage, decide_status


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
