import json
import os
import sys

import pytest

from conftest import FAKE_HARNESS
from phd import Dispatcher, Inputs, RunStatus

FAKE = [sys.executable, str(FAKE_HARNESS)]
SCHEMA = {"type": "object", "properties": {"event": {"enum": ["done", "blocked"]}}, "required": ["event"]}
MISSING = object()


def claude_stream(tmp_path, name, *, result, structured=MISSING, session="sess-s1", cost=0.01):
    final = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": result,
             "session_id": session, "total_cost_usd": cost, "usage": {"input_tokens": 10, "output_tokens": 5}}
    if structured is not MISSING:
        final["structured_output"] = structured
    path = tmp_path / f"{name}.jsonl"
    lines = [{"type": "system", "subtype": "init", "session_id": session, "claude_code_version": "2.1.286"}, final]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return path


def run(tmp_path, make_spec, **kw):
    d = Dispatcher(make_spec("claude", output_schema=SCHEMA, **kw), runs_root=tmp_path / "runs",
                   validate_model=False, allow_temp_runs_root=True, executables={"claude": FAKE})
    return d, d.spawn(Inputs(msg="x"))


@pytest.fixture
def stream(monkeypatch):
    def use(*paths):
        if len(paths) == 1:
            monkeypatch.setenv("FAKE_STREAM", str(paths[0]))
        else:
            monkeypatch.setenv("FAKE_STREAMS", os.pathsep.join(str(p) for p in paths))
            monkeypatch.setenv("FAKE_STATE", str(paths[0].parent / "state"))
    return use


@pytest.fixture
def echo(tmp_path, monkeypatch):
    path = tmp_path / "echo.jsonl"
    monkeypatch.setenv("FAKE_ECHO_FILE", str(path))
    return lambda: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_native_structured_output_that_matches(tmp_path, make_spec, stream):
    stream(claude_stream(tmp_path, "ok", result="", structured={"event": "done"}))
    _, r = run(tmp_path, make_spec)
    assert r.status is RunStatus.SUCCEEDED and r.structured_output == {"event": "done"}


def test_native_structured_output_that_does_not_match(tmp_path, make_spec, stream):
    stream(claude_stream(tmp_path, "bad", result="", structured={"event": "nope"}))
    _, r = run(tmp_path, make_spec)
    assert r.status is RunStatus.FAILED and r.error.kind == "schema"
    assert r.error.message.startswith("$.event: ") and r.structured_output == {"event": "nope"}


@pytest.mark.parametrize("text", ['{"event": "done"}', 'Sure:\n```json\n{"event": "done"}\n```'])
def test_json_in_the_result_text(tmp_path, make_spec, stream, text):
    stream(claude_stream(tmp_path, "text", result=text))
    _, r = run(tmp_path, make_spec)
    assert r.status is RunStatus.SUCCEEDED and r.structured_output == {"event": "done"}


def test_non_json_answer(tmp_path, make_spec, stream):
    stream(claude_stream(tmp_path, "prose", result="I finished the task."))
    _, r = run(tmp_path, make_spec)
    assert r.error.kind == "schema" and r.error.message == "final answer is not JSON"


def test_a_failed_run_is_not_validated(tmp_path, make_spec, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "3")
    _, r = run(tmp_path, make_spec)
    assert r.error.kind == "harness_crash"


def test_repair_turn_fixes_the_answer(tmp_path, make_spec, stream, echo):
    stream(claude_stream(tmp_path, "bad", result="", structured={"event": "nope"}),
           claude_stream(tmp_path, "good", result="", structured={"event": "done"}))
    _, r = run(tmp_path, make_spec, schema_repair=True)
    assert r.status is RunStatus.SUCCEEDED and r.structured_output == {"event": "done"}
    assert "schema repair turn used" in r.warnings and r.cost_usd == pytest.approx(0.02) and r.num_turns == 2
    calls = echo()
    assert len(calls) == 2 and "--resume" in calls[1]["argv"] and "sess-s1" in calls[1]["argv"]
    assert calls[1]["stdin"].startswith("Your final answer did not match the required JSON Schema: $.event")
    types = [json.loads(line)["type"] for line in open(r.events_path, encoding="utf-8")]
    assert types.count("schema.repair") == 1 and types.count("run.finished") == 1 and types[-1] == "run.finished"


def test_repair_that_fails_again(tmp_path, make_spec, stream):
    bad = claude_stream(tmp_path, "bad", result="", structured={"event": "nope"})
    stream(bad, bad)
    _, r = run(tmp_path, make_spec, schema_repair=True)
    assert r.error.kind == "schema" and r.error.message.startswith("after one repair turn: $.event")


def test_repair_is_off_by_default(tmp_path, make_spec, stream, echo):
    stream(claude_stream(tmp_path, "bad", result="", structured={"event": "nope"}),
           claude_stream(tmp_path, "good", result="", structured={"event": "done"}))
    _, r = run(tmp_path, make_spec)
    assert r.error.kind == "schema" and len(echo()) == 1


def test_repair_shares_the_budget(tmp_path, make_spec, stream):
    stream(claude_stream(tmp_path, "bad", result="", structured={"event": "nope"}),
           claude_stream(tmp_path, "good", result="", structured={"event": "done"}))
    _, r = run(tmp_path, make_spec, schema_repair=True, budget_usd=0.015)
    assert r.status is RunStatus.BUDGET_EXCEEDED


def test_repair_works_with_resumable_false(tmp_path, make_spec, stream):  # Review Focus 3
    stream(claude_stream(tmp_path, "bad", result="", structured={"event": "nope"}),
           claude_stream(tmp_path, "good", result="", structured={"event": "done"}))
    _, r = run(tmp_path, make_spec, schema_repair=True, resumable=False)
    assert r.status is RunStatus.SUCCEEDED and not os.path.exists(r.home_dir)


def test_repair_on_a_resumed_turn_reuses_resume_argv(tmp_path, make_spec, stream, echo):  # Review Focus 4
    good = claude_stream(tmp_path, "good", result="", structured={"event": "done"})
    bad = claude_stream(tmp_path, "bad", result="", structured={"event": "nope"})
    stream(good, bad, good)
    d, first = run(tmp_path, make_spec, schema_repair=True)
    r = d.spawn(Inputs(msg="again"), session_id=first.session_id)
    assert r.status is RunStatus.SUCCEEDED
    assert echo()[2]["argv"].count("--resume") == 1


def test_schema_error_message_is_scrubbed(tmp_path, make_spec, stream):  # Review Focus 5
    stream(claude_stream(tmp_path, "leak", result="", structured={"event": "phd-echo-secret"}))
    _, r = run(tmp_path, make_spec, secrets={"ECHO_PREFIX": "phd-echo-secret"})
    assert r.error.kind == "schema" and "phd-echo-secret" not in r.error.message
