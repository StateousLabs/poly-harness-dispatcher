"""Dispatcher + fake harness: the translated schema reaches the CLI and the answer comes back in the caller's shape."""

import json
import sys

import pytest

from conftest import FAKE_HARNESS
from phd import Dispatcher, Inputs, RunStatus

FAKE = [sys.executable, str(FAKE_HARNESS)]
TWO = {"oneOf": [
    {"type": "object", "properties": {"event": {"const": "done"}, "payload": {"type": "object", "properties": {"a": {"type": "integer"}}}},
     "required": ["event"], "additionalProperties": False},
    {"type": "object", "properties": {"event": {"const": "blocked"}}, "required": ["event"],
     "additionalProperties": False}]}
OBJ = {"type": "object", "properties": {"event": {"enum": ["done", "blocked"]}}, "required": ["event"]}


@pytest.fixture
def echo(tmp_path, monkeypatch):
    path = tmp_path / "echo.jsonl"
    monkeypatch.setenv("FAKE_ECHO_FILE", str(path))
    return lambda: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_stream(tmp_path, monkeypatch, lines):
    path = tmp_path / "stream.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(path))


def claude_lines(structured):
    return [{"type": "system", "subtype": "init", "session_id": "s1", "claude_code_version": "2.1.286"},
            {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "",
             "session_id": "s1", "total_cost_usd": 0.01, "structured_output": structured,
             "usage": {"input_tokens": 1, "output_tokens": 1}}]


def codex_lines(answer):
    return [{"type": "thread.started", "thread_id": "t1"},
            {"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": json.dumps(answer)}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]


def run(tmp_path, make_spec, harness, schema):
    d = Dispatcher(make_spec(harness, output_schema=schema), runs_root=tmp_path / "runs", validate_model=False,
                   allow_temp_runs_root=True, executables={harness: FAKE})
    return d.spawn(Inputs(msg="x"))


def flag_value(call, flag):
    return call["argv"][call["argv"].index(flag) + 1]


def test_claude_root_oneof_is_wrapped_and_restored(tmp_path, monkeypatch, make_spec, echo):
    write_stream(tmp_path, monkeypatch, claude_lines({"outcome": {"event": "done", "payload": {"a": 1}}}))
    r = run(tmp_path, make_spec, "claude", TWO)
    sent = json.loads(flag_value(echo()[0], "--json-schema"))
    assert sent["type"] == "object" and sent["properties"] == {"outcome": TWO} and sent["required"] == ["outcome"]
    assert r.status is RunStatus.SUCCEEDED and r.structured_output == {"event": "done", "payload": {"a": 1}}


def test_claude_wrapped_answer_violating_the_callers_schema(tmp_path, monkeypatch, make_spec):
    write_stream(tmp_path, monkeypatch, claude_lines({"outcome": {"event": "nope"}}))
    r = run(tmp_path, make_spec, "claude", TWO)
    assert r.status is RunStatus.FAILED and r.error.kind == "schema" and r.structured_output == {"event": "nope"}


def test_claude_object_root_is_sent_as_given(tmp_path, monkeypatch, make_spec, echo):
    write_stream(tmp_path, monkeypatch, claude_lines({"event": "done"}))
    r = run(tmp_path, make_spec, "claude", OBJ)
    assert json.loads(flag_value(echo()[0], "--json-schema")) == OBJ
    assert r.status is RunStatus.SUCCEEDED and r.structured_output == {"event": "done"}


def test_codex_schema_file_is_strict_and_null_payload_removed(tmp_path, monkeypatch, make_spec, echo):
    write_stream(tmp_path, monkeypatch, codex_lines({"outcome": {"event": "done", "payload": None}}))
    r = run(tmp_path, make_spec, "codex", TWO)
    path = flag_value(echo()[0], "--output-schema")
    written = json.loads(open(path, encoding="utf-8").read())
    done = written["properties"]["outcome"]["anyOf"][0]
    assert written["required"] == ["outcome"] and written["additionalProperties"] is False
    assert done["required"] == ["event", "payload"] and done["additionalProperties"] is False
    assert done["properties"]["payload"]["anyOf"][1] == {"type": "null"}
    assert r.status is RunStatus.SUCCEEDED and r.structured_output == {"event": "done"}


def test_codex_translated_file_content(tmp_path, make_spec):
    """The schema file materialize writes for codex is the strict form; restore_answer undoes it."""
    from phd.harnesses import get_adapter
    from helpers import materialize_spec
    m = materialize_spec(make_spec("codex", output_schema=TWO), Inputs(msg="x"), run_id="r", run_dir=tmp_path / "r")
    written = json.loads(open(flag_value({"argv": m.argv}, "--output-schema"), encoding="utf-8").read())
    assert written["properties"]["outcome"]["anyOf"][0]["required"] == ["event", "payload"]
    assert written["additionalProperties"] is False and "$schema" not in written
    assert get_adapter("codex").restore_answer(make_spec("codex", output_schema=TWO),
                                               {"outcome": {"event": "done", "payload": None}}) == {"event": "done"}
