import json
import os
import sys
import time

import pytest

from conftest import FAKE_HARNESS
from phd import Dispatcher, Inputs, RunStatus
from phd.execution.handle import _merge, _with_budget

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
           claude_stream(tmp_path, "good", result="", structured={"event": "done"}, cost=0.02))  # cumulative
    _, r = run(tmp_path, make_spec, schema_repair=True)
    assert r.status is RunStatus.SUCCEEDED and r.structured_output == {"event": "done"}
    assert "schema repair turn used" in r.warnings and r.cost_usd == pytest.approx(0.02) and r.num_turns == 2
    assert r.session_cost_usd == pytest.approx(0.02)
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


@pytest.mark.parametrize("second_cumulative,status", [(0.02, RunStatus.BUDGET_EXCEEDED),
                                                       (0.014, RunStatus.SUCCEEDED)])
def test_repair_shares_the_budget(tmp_path, make_spec, stream, second_cumulative, status):
    # per-turn 0.01 + (second_cumulative - 0.01): a sum of 0.02 exceeds 0.015, 0.014 does not
    stream(claude_stream(tmp_path, "bad", result="", structured={"event": "nope"}),
           claude_stream(tmp_path, "good", result="", structured={"event": "done"}, cost=second_cumulative))
    _, r = run(tmp_path, make_spec, schema_repair=True, budget_usd=0.015)
    assert r.status is status and r.cost_usd == pytest.approx(second_cumulative)


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


@pytest.fixture
def on_second_spawn(monkeypatch):
    """Run `action(handle)` instead of / before the second harness spawn of a run."""
    from phd.execution.handle import RunHandle
    original = RunHandle._new_process

    def install(action):
        calls = []

        def patched(self, argv, stdin_text):
            calls.append(argv)
            if len(calls) == 2:
                action(self)
            return original(self, argv, stdin_text)
        monkeypatch.setattr(RunHandle, "_new_process", patched)
    return install


def two_streams(tmp_path, first_cost=0.01):
    """A wrong first answer, then a corrected one costing 0.01 more (claude reports the session total)."""
    return (claude_stream(tmp_path, "bad", result="", structured={"event": "nope"}, cost=first_cost),
            claude_stream(tmp_path, "good", result="", structured={"event": "done"}, cost=first_cost + 0.01))


def test_cancel_during_repair_start_is_not_lost(tmp_path, make_spec, stream, monkeypatch, on_second_spawn):
    stream(*two_streams(tmp_path))

    def cancel(handle):
        monkeypatch.setenv("FAKE_SLEEP", "30")
        handle._request_cancel()
    on_second_spawn(cancel)
    t0 = time.monotonic()
    _, r = run(tmp_path, make_spec, schema_repair=True)
    assert r.status is RunStatus.CANCELLED and time.monotonic() - t0 < 15


def test_repair_spawn_failure_keeps_the_first_turn(tmp_path, make_spec, stream, on_second_spawn):
    stream(*two_streams(tmp_path))

    def boom(handle):
        raise OSError("no such harness")
    on_second_spawn(boom)
    _, r = run(tmp_path, make_spec, schema_repair=True, resumable=False)
    assert r.error.kind == "schema" and r.session_id == "sess-s1" and r.cost_usd == pytest.approx(0.01)
    assert "schema repair turn could not start: OSError: no such harness" in r.warnings
    types = [json.loads(line)["type"] for line in open(r.events_path, encoding="utf-8")]
    assert types[-1] == "run.finished" and not os.path.exists(r.home_dir)


def test_repair_gets_only_the_remaining_budget(tmp_path, make_spec, stream, echo):
    stream(*two_streams(tmp_path))
    _, r = run(tmp_path, make_spec, schema_repair=True, budget_usd=0.025)
    assert r.status is RunStatus.SUCCEEDED
    calls = echo()
    argv = calls[1]["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.015"
    assert calls[0]["argv"][calls[0]["argv"].index("--max-budget-usd") + 1] == "0.025"


def test_no_repair_when_the_budget_is_spent(tmp_path, make_spec, stream, echo):
    stream(*two_streams(tmp_path, first_cost=0.02))
    _, r = run(tmp_path, make_spec, schema_repair=True, budget_usd=0.02)
    assert len(echo()) == 1 and r.error.kind == "schema"
    assert "schema repair skipped: budget_usd spent" in r.warnings


def test_no_repair_when_the_timeout_is_nearly_spent(tmp_path, make_spec, stream, echo, monkeypatch):
    from phd.execution import handle as handle_mod
    stream(*two_streams(tmp_path))
    monkeypatch.setattr(handle_mod, "REPAIR_MIN_TIME_S", 10_000.0)  # more than timeout_s leaves after any first turn
    _, r = run(tmp_path, make_spec, schema_repair=True, timeout_s=1800)
    assert len(echo()) == 1 and r.error.kind == "schema"
    assert "schema repair skipped: timeout_s nearly spent" in r.warnings
    types = [json.loads(line)["type"] for line in open(r.events_path, encoding="utf-8")]
    assert "schema.repair" not in types


def test_repair_failure_after_start_keeps_the_first_turn(tmp_path, make_spec, stream, monkeypatch):
    from phd.execution import handle as handle_mod
    stream(*two_streams(tmp_path))

    def boom(first, second):
        raise ValueError("merge exploded")
    monkeypatch.setattr(handle_mod, "_merge", boom)
    _, r = run(tmp_path, make_spec, schema_repair=True)
    assert r.error.kind == "schema" and r.cost_usd == pytest.approx(0.01)
    assert "schema repair turn failed: ValueError: merge exploded" in r.warnings
    types = [json.loads(line)["type"] for line in open(r.events_path, encoding="utf-8")]
    assert types[-1] == "run.finished" and types.count("run.finished") == 1


def test_schema_repair_event_only_after_the_repair_started(tmp_path, make_spec, stream, on_second_spawn):
    stream(*two_streams(tmp_path))

    def boom(handle):
        raise OSError("no such harness")
    on_second_spawn(boom)
    _, r = run(tmp_path, make_spec, schema_repair=True)
    types = [json.loads(line)["type"] for line in open(r.events_path, encoding="utf-8")]
    assert "schema.repair" not in types


def test_resumed_claude_turn_reports_its_own_cost(tmp_path, make_spec, stream):
    good = {"result": "", "structured": {"event": "done"}}
    stream(claude_stream(tmp_path, "t1", cost=0.02, **good), claude_stream(tmp_path, "t2", cost=0.03, **good),
           claude_stream(tmp_path, "t3", cost=0.05, **good))
    d, first = run(tmp_path, make_spec, budget_usd=0.025)
    assert first.cost_usd == pytest.approx(0.02) and first.session_cost_usd == pytest.approx(0.02)
    second = d.spawn(Inputs(msg="again"), session_id=first.session_id)
    assert second.status is RunStatus.SUCCEEDED  # 0.01 this turn, not the session's 0.03
    assert second.cost_usd == pytest.approx(0.01) and second.session_cost_usd == pytest.approx(0.03)
    third = d.spawn(Inputs(msg="and again"), session_id=first.session_id)
    assert third.cost_usd == pytest.approx(0.02) and third.session_cost_usd == pytest.approx(0.05)


def test_resume_after_an_old_record_falls_back_to_its_cost(tmp_path, make_spec, stream):
    good = {"result": "", "structured": {"event": "done"}}
    stream(claude_stream(tmp_path, "t1", cost=0.02, **good), claude_stream(tmp_path, "t2", cost=0.03, **good))
    d, first = run(tmp_path, make_spec)
    path = os.path.join(first.run_dir, "result.json")
    with open(path, encoding="utf-8") as f:
        record = json.load(f)
    del record["session_cost_usd"]  # written before the field existed
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f)
    second = d.spawn(Inputs(msg="again"), session_id=first.session_id)
    assert second.cost_usd == pytest.approx(0.01) and second.session_cost_usd == pytest.approx(0.03)


def test_per_invocation_cost_harness_is_unaffected_on_resume(tmp_path, make_spec, monkeypatch):
    from conftest import STREAMS
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "opencode_ok.jsonl"))
    d = Dispatcher(make_spec("opencode"), runs_root=tmp_path / "runs", validate_model=False,
                   allow_temp_runs_root=True, executables={"opencode": FAKE})
    first = d.spawn(Inputs(msg="x"))
    second = d.spawn(Inputs(msg="again"), session_id=first.session_id)
    assert first.cost_usd and second.cost_usd == pytest.approx(first.cost_usd)
    assert first.session_cost_usd is None and second.session_cost_usd is None


def test_escaped_secret_renderings_are_scrubbed(tmp_path, make_spec, stream):
    secret = "it's\\secret"  # a quote and a backslash: jsonschema prints repr(), json would double the backslash
    stream(claude_stream(tmp_path, "leak", result="", structured={"event": secret}))
    _, r = run(tmp_path, make_spec, secrets={"ECHO_PREFIX": secret})
    assert r.error.kind == "schema" and "secret" not in r.error.message


def test_with_budget_helper():
    assert _with_budget(["x", "--max-budget-usd", "1.0", "--y"], 0.3) == ["x", "--max-budget-usd", "0.3", "--y"]
    assert _with_budget(["x"], 0.3) == ["x"]


def test_merge_with_one_unpriced_turn_is_unknown():
    from phd.harnesses import Outcome
    priced = Outcome(cost_usd=0.01, cost_source="harness")
    unpriced = Outcome()
    assert _merge(priced, unpriced).cost_source == "unknown"
    assert _merge(priced, priced).cost_source == "harness"
