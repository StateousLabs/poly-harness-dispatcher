"""A session that cannot be resumed comes back as a failed run with kind="session"; nothing is launched."""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from conftest import FAKE_HARNESS, STREAMS
from phd import Dispatcher, Inputs, RunStatus
from phd.errors import NotResumable, UnsupportedFeature
from phd.harnesses.outcome import classify_error

FAKE = [sys.executable, str(FAKE_HARNESS)]

# What the real CLIs print for a session they no longer have (captured with a made-up id).
CLAUDE_RESULT = {"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
                 "session_id": "00000000-0000-0000-0000-000000000000", "total_cost_usd": 0, "usage": {},
                 "errors": ["No conversation found with session ID: 00000000-0000-0000-0000-000000000000"]}
CODEX_STDERR = ("Error: thread/resume: thread/resume failed: no rollout found for thread id "
                "00000000-0000-0000-0000-000000000000 (code -32600)\n")
OPENCODE_STDERR = "\x1b[91m\x1b[1mError: \x1b[0mSession not found\n"


def dispatcher(spec, tmp_path, **kw):
    kw.setdefault("sinks", ())
    return Dispatcher(spec, runs_root=tmp_path / "runs", validate_model=False, allow_temp_runs_root=True,
                      executables={spec.harness: FAKE}, **kw)


@pytest.fixture
def echo(tmp_path, monkeypatch):
    path = tmp_path / "echo.jsonl"
    monkeypatch.setenv("FAKE_ECHO_FILE", str(path))
    return path


def make_cases(tmp_path, monkeypatch, make_spec, inputs):
    """The five unresumable cases as (name, dispatcher, session_id, expected message part)."""
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(inputs)
    other = tmp_path / "elsewhere"
    other.mkdir()
    other_stream = tmp_path / "other.jsonl"  # a second session, whose run discards its home
    other_stream.write_text((STREAMS / "claude_ok.jsonl").read_text(encoding="utf-8").replace("sess-claude-1", "sess-gone"),
                            encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(other_stream))
    gone = dispatcher(make_spec("claude", resumable=False), tmp_path).spawn(Inputs(msg="x"))
    return [("unknown", d, "no-such-session", "no-such-session"),
            ("other harness", dispatcher(make_spec("opencode"), tmp_path), first.session_id, "belongs to"),
            ("other cwd", dispatcher(make_spec("claude", cwd=other), tmp_path), first.session_id, "ran in cwd"),
            ("home gone", d, gone.session_id, "no longer exists")]


def test_each_unresumable_case_is_a_failed_session_run(tmp_path, monkeypatch, make_spec, inputs, echo):
    cases = make_cases(tmp_path, monkeypatch, make_spec, inputs)
    echo.unlink()  # forget the two setup runs: nothing below may launch a harness
    for name, d, sid, part in cases:
        r = d.spawn(Inputs(msg="again"), session_id=sid)
        assert r.status is RunStatus.FAILED and r.error.kind == "session", name
        assert part in r.error.message and r.session_id is None and not r.resumable and r.home_dir == ""
        assert (Path(r.run_dir) / "result.json").is_file()
        assert d.ledger.get(r.run_id).error.kind == "session"
        record = d.ledger.read_record(r.run_id)
        assert record["session_id"] == sid and record["argv"] == []
    assert not echo.exists()


async def test_the_failed_run_streams_its_events_and_reaches_the_sinks(tmp_path, monkeypatch, make_spec, inputs):
    seen = []

    class Sink:
        async def put(self, result):
            seen.append(result.run_id)

    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path, sinks=[Sink()])
    async with d.run(Inputs(msg="hi"), session_id="no-such-session") as handle:
        assert handle.done
        result = await handle.wait()
        assert [e.type async for e in handle.events()] == ["run.started", "run.finished"]
        assert (await handle.cancel()) is result
    assert result.error.kind == "session" and seen == [result.run_id]
    lines = (Path(result.run_dir) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["type"] for line in lines] == ["run.started", "run.finished"]
    assert [r.run_id for r in d.ledger.list()] == [result.run_id]


def test_not_a_session_gone_problem_still_raises(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(inputs)
    with pytest.raises(UnsupportedFeature):
        d.spawn(inputs, session_id=first.session_id)  # inputs carries rules
    with pytest.raises(NotResumable, match="no-such-session"):
        d.plan(Inputs(msg="hi"), session_id="no-such-session")
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _link_inside_home_is_refused(tmp_path, d, first)
    result_path = Path(first.run_dir) / "result.json"
    data = json.loads(result_path.read_text(encoding="utf-8")) | {"home_dir": str(decoy)}
    result_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(NotResumable, match="not a run home"):
        d.spawn(Inputs(msg="hi"), session_id=first.session_id)


def _link_inside_home_is_refused(tmp_path, d, first):
    skills = Path(first.home_dir) / "skills"
    target = tmp_path / "precious"
    target.mkdir()
    shutil.rmtree(skills)
    try:
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(target), str(skills))
        else:
            skills.symlink_to(target, target_is_directory=True)
    except OSError as e:
        pytest.skip(f"cannot create links here: {e}")
    with pytest.raises(NotResumable, match="symlink or junction"):
        d.spawn(Inputs(msg="hi"), session_id=first.session_id)
    os.rmdir(skills) if sys.platform == "win32" else skills.unlink()
    skills.mkdir()


def _expired(tmp_path, monkeypatch, make_spec, harness, ok_stream):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / ok_stream))
    d = dispatcher(make_spec(harness), tmp_path)
    first = d.spawn(Inputs(msg="hi"))
    assert first.session_id
    return d, first


def test_claude_reporting_a_missing_session_is_a_session_error(tmp_path, monkeypatch, make_spec):
    d, first = _expired(tmp_path, monkeypatch, make_spec, "claude", "claude_ok.jsonl")
    stream = tmp_path / "gone.jsonl"
    stream.write_text(json.dumps(CLAUDE_RESULT) + "\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(stream))
    monkeypatch.setenv("FAKE_EXIT", "1")
    r = d.spawn(Inputs(msg="again"), session_id=first.session_id)
    assert r.status is RunStatus.FAILED and r.error.kind == "session" and "No conversation found" in r.error.message


@pytest.mark.parametrize("harness,ok_stream,stderr", [("codex", "codex_ok.jsonl", CODEX_STDERR),
                                                      ("opencode", "opencode_ok.jsonl", OPENCODE_STDERR)])
def test_stderr_only_session_errors_are_session_errors(tmp_path, monkeypatch, make_spec, harness, ok_stream, stderr):
    d, first = _expired(tmp_path, monkeypatch, make_spec, harness, ok_stream)
    monkeypatch.delenv("FAKE_STREAM")
    monkeypatch.setenv("FAKE_STDERR", stderr)
    monkeypatch.setenv("FAKE_EXIT", "1")
    r = d.spawn(Inputs(msg="again"), session_id=first.session_id)
    assert r.status is RunStatus.FAILED and r.error.kind == "session" and r.error.message.startswith("exit code 1")
    # the same crash on a fresh run is not a session problem
    fresh = d.spawn(Inputs(msg="hi"))
    assert fresh.error.kind == "harness_crash"


def test_classify_error_knows_the_real_messages():
    assert classify_error(CLAUDE_RESULT["errors"][0]) == "session"
    assert classify_error(CODEX_STDERR) == "session"
    assert classify_error(OPENCODE_STDERR) == "session"
    assert classify_error("model foo not found") == "model_unavailable"


MCP_MESSAGE = 'MCP error: Session not found (mcp server "x")'


def test_mcp_style_session_messages_are_not_session_errors():
    for text in (MCP_MESSAGE, "Session not found", "Error: Session not found (mcp server x)",
                 "no rollout found for thread id 1", "No conversation found"):
        assert classify_error(text) != "session", text
    assert classify_error("log line" + chr(10) + OPENCODE_STDERR) == "session"


@pytest.mark.parametrize("harness,ok_stream,line", [
    ("codex", "codex_ok.jsonl", {"type": "error", "message": MCP_MESSAGE}),
    ("opencode", "opencode_ok.jsonl", {"type": "error", "error": {"data": {"message": MCP_MESSAGE}}})])
def test_an_mcp_message_in_a_fresh_runs_stream_is_not_a_session_error(tmp_path, monkeypatch, make_spec, harness,
                                                                     ok_stream, line):
    stream = tmp_path / "mcp.jsonl"
    stream.write_text(json.dumps(line) + "\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(stream))
    monkeypatch.setenv("FAKE_EXIT", "1")
    r = dispatcher(make_spec(harness), tmp_path).spawn(Inputs(msg="hi"))
    assert r.error.kind != "session"


def test_the_claude_message_on_a_fresh_run_is_a_harness_error(tmp_path, monkeypatch, make_spec):
    stream = tmp_path / "gone.jsonl"
    stream.write_text(json.dumps(CLAUDE_RESULT) + "\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(stream))
    monkeypatch.setenv("FAKE_EXIT", "1")
    r = dispatcher(make_spec("claude"), tmp_path).spawn(Inputs(msg="hi"))
    assert r.error.kind == "harness_error" and "No conversation found" in r.error.message


@pytest.mark.parametrize("harness,ok_stream", [("codex", "codex_ok.jsonl"), ("opencode", "opencode_ok.jsonl")])
def test_an_mcp_message_in_a_resumed_crash_stays_a_harness_crash(tmp_path, monkeypatch, make_spec, harness, ok_stream):
    d, first = _expired(tmp_path, monkeypatch, make_spec, harness, ok_stream)
    monkeypatch.delenv("FAKE_STREAM")
    monkeypatch.setenv("FAKE_STDERR", MCP_MESSAGE + "\n")
    monkeypatch.setenv("FAKE_EXIT", "1")
    r = d.spawn(Inputs(msg="again"), session_id=first.session_id)
    assert r.error.kind == "harness_crash"


def test_claude_errors_fallback_is_limited_to_error_during_execution(tmp_path, monkeypatch, make_spec):
    other = CLAUDE_RESULT | {"subtype": "error_max_turns"}
    stream = tmp_path / "turns.jsonl"
    stream.write_text(json.dumps(other) + "\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(stream))
    monkeypatch.setenv("FAKE_EXIT", "1")
    r = dispatcher(make_spec("claude"), tmp_path).spawn(Inputs(msg="hi"))
    assert r.error.message == "error_max_turns"
