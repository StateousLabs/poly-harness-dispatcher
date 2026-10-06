import json

from phd.results import RunError, RunResult, RunStatus, Usage
from phd.results.model import decide_status


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


def test_new_error_kinds_map_to_statuses():
    assert decide_status(RunError("idle_timeout", "x")) is RunStatus.TIMED_OUT
    assert decide_status(RunError("schema", "x")) is RunStatus.FAILED
