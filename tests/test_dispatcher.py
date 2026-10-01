import asyncio
import json
import sys
import time

import pytest

from conftest import FAKE_HARNESS, STREAMS
from helpers import wait_dead
from phd import Dispatcher, Inputs, RunStatus
from phd.errors import InvalidRunsRoot, NotResumable

FAKE = [sys.executable, str(FAKE_HARNESS)]


def dispatcher(spec, tmp_path, **kw):
    kw.setdefault("runs_root", tmp_path / "runs")
    kw.setdefault("validate_model", False)
    kw.setdefault("allow_temp_runs_root", True)
    return Dispatcher(spec, executables={spec.harness: FAKE}, **kw)


@pytest.fixture
def echo(tmp_path, monkeypatch):
    path = tmp_path / "echo.jsonl"
    monkeypatch.setenv("FAKE_ECHO_FILE", str(path))
    return lambda: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_claude_spawn_success(tmp_path, monkeypatch, make_spec, inputs, echo, workdir):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-leak")          # Review Focus 1
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/host/.claude")
    d = dispatcher(make_spec("claude"), tmp_path)
    r = d.spawn(inputs)

    assert r.status is RunStatus.SUCCEEDED and r.error is None
    assert r.session_id == "sess-claude-1" and r.cost_usd == pytest.approx(0.0284) and r.cli_version == "2.1.286"
    assert r.result_text.startswith("HELPER-IDENTITY-OK") and r.resumable
    assert r.stack == "fixture-helper@0.1.0" and r.stack_hash.startswith("sha256:")
    assert r.cwd == str(workdir) and r.credential_kind == "claude_oauth_token"

    call = echo()[0]
    assert call["argv"][0] == "-p" and "--append-system-prompt-file" in call["argv"]
    assert call["stdin"].startswith("# Task brief") and "café" in call["stdin"]
    assert call["cwd"] == str(workdir)
    assert call["env"]["CLAUDE_CONFIG_DIR"] == r.home_dir and call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "test-oauth-token"
    assert "ANTHROPIC_API_KEY" not in call["env"]

    assert d.ledger.get(r.run_id).to_dict() == r.to_dict()
    record = json.loads((tmp_path / "runs" / r.run_id / "run.json").read_text(encoding="utf-8"))
    assert record["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "<redacted>"
    assert "test-oauth-token" not in (tmp_path / "runs" / r.run_id / "result.json").read_text(encoding="utf-8")
    types = [json.loads(line)["type"] for line in open(r.events_path, encoding="utf-8")]
    assert types[0] == "run.started" and types[-1] == "run.finished" and "tool_call" in types


async def test_events_stream(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "opencode_ok.jsonl"))
    handle = await dispatcher(make_spec("opencode"), tmp_path).start(inputs)
    types = [ev.type async for ev in handle.events()]
    assert types == ["run.started", "tool_call", "usage", "message", "usage", "run.finished"]
    assert (await handle.wait()).status is RunStatus.SUCCEEDED


def test_non_json_stdout_becomes_log_event(tmp_path, monkeypatch, make_spec, inputs):  # Review Focus 3
    stream = tmp_path / "noisy.jsonl"
    stream.write_text("Reading additional input from stdin...\n" + (STREAMS / "codex_ok.jsonl").read_text(encoding="utf-8"),
                      encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(stream))
    r = dispatcher(make_spec("codex"), tmp_path).spawn(inputs)
    assert r.status is RunStatus.SUCCEEDED and r.cost_source == "unknown"
    logs = [json.loads(l) for l in open(r.events_path, encoding="utf-8") if json.loads(l)["type"] == "log"]
    assert logs[0]["data"]["text"] == "Reading additional input from stdin..."


def test_resume_reuses_home_and_session(tmp_path, monkeypatch, make_spec, inputs, echo):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(inputs)
    second = d.resume(first.run_id, "now fix the test")
    third = d.resume(second, "and again")
    calls = echo()
    assert calls[1]["argv"][-2:] == ["--resume", "sess-claude-1"]
    assert calls[2]["argv"].count("--resume") == 1
    assert calls[1]["stdin"] == "# Message\n\nnow fix the test\n"
    assert second.home_dir == first.home_dir and second.resumed_from == first.run_id
    assert third.resumed_from == second.run_id and second.run_id != first.run_id


def test_codex_resume_command(tmp_path, monkeypatch, make_spec, inputs, echo, workdir):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "codex_ok.jsonl"))
    d = dispatcher(make_spec("codex"), tmp_path)
    d.resume(d.spawn(inputs), "continue")
    resumed = echo()[1]
    assert resumed["argv"][:3] == ["exec", "resume", "01a0f55a-d4f3-7d43-943f-8708f4dfa0cf"]
    assert "-C" not in resumed["argv"] and resumed["cwd"] == str(workdir)


def test_not_resumable_without_home(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude", resumable=False), tmp_path)
    r = d.spawn(inputs)
    assert not r.resumable
    from pathlib import Path
    assert not Path(r.home_dir).exists() and (Path(r.run_dir) / "result.json").is_file()
    with pytest.raises(NotResumable, match="no longer exists"):
        d.resume(r, "more")


def test_auth_failure_status(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_not_logged_in.jsonl"))
    monkeypatch.setenv("FAKE_EXIT", "1")
    r = dispatcher(make_spec("claude"), tmp_path).spawn(inputs)
    assert r.status is RunStatus.AUTH_FAILED and r.error.kind == "auth" and r.exit_code == 1


def test_crash_without_output(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_EXIT", "3")
    r = dispatcher(make_spec("claude"), tmp_path).spawn(inputs)
    assert r.status is RunStatus.FAILED and r.error.kind == "harness_crash" and "exit code 3" in r.error.message


def test_timeout_kills_tree(tmp_path, monkeypatch, make_spec, inputs):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CHILD_PIDFILE", str(pidfile))
    monkeypatch.setenv("FAKE_SLEEP", "60")
    t0 = time.monotonic()
    r = dispatcher(make_spec("claude", timeout_s=2), tmp_path).spawn(inputs)
    assert r.status is RunStatus.TIMED_OUT and time.monotonic() - t0 < 20
    assert wait_dead(int(pidfile.read_text()))


async def test_cancel(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_SLEEP", "60")
    handle = await dispatcher(make_spec("claude"), tmp_path).start(inputs)
    await asyncio.sleep(0.5)
    r = await handle.cancel()
    assert r.status is RunStatus.CANCELLED


def test_budget_enforced_from_stream(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "opencode_costly.jsonl"))
    monkeypatch.setenv("FAKE_SLEEP", "60")
    r = dispatcher(make_spec("opencode", budget_usd=0.1), tmp_path).spawn(inputs)
    assert r.status is RunStatus.BUDGET_EXCEEDED and r.cost_usd == pytest.approx(0.5)


def test_codex_refuses_temp_runs_root(tmp_path, make_spec):
    with pytest.raises(InvalidRunsRoot):
        Dispatcher(make_spec("codex"), runs_root=tmp_path / "runs", validate_model=False)


async def test_sync_spawn_inside_event_loop_raises(tmp_path, make_spec, inputs):
    with pytest.raises(RuntimeError, match="await dispatcher.start"):
        dispatcher(make_spec("claude"), tmp_path).spawn(inputs)


def test_plan_writes_home_without_running(tmp_path, make_spec, inputs, echo):
    m = dispatcher(make_spec("claude"), tmp_path).plan(inputs)
    assert (m.home / "settings.json").is_file() and (m.run_dir / "run.json").is_file()
    assert not (tmp_path / "echo.jsonl").exists()


def test_structured_output_from_result_text(tmp_path, monkeypatch, make_spec, inputs):
    stream = tmp_path / "codex_json.jsonl"
    stream.write_text('{"type":"thread.started","thread_id":"t1"}\n'
                      '{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"{\\"ok\\": true}"}}\n'
                      '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n', encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(stream))
    r = dispatcher(make_spec("codex", output_schema={"type": "object"}), tmp_path).spawn(inputs)
    assert r.structured_output == {"ok": True}


def test_stream_overflow_fails_run(tmp_path, monkeypatch, make_spec, inputs):
    import phd.launcher
    monkeypatch.setattr(phd.launcher, "STREAM_LIMIT", 1024)
    stream = tmp_path / "big.jsonl"
    stream.write_text(json.dumps({"type": "x", "pad": "a" * 10240}) + "\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(stream))
    r = dispatcher(make_spec("claude"), tmp_path).spawn(inputs)
    assert r.status is RunStatus.FAILED and r.error.kind == "stream_overflow"


async def test_events_second_iteration_terminates(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "opencode_ok.jsonl"))
    handle = await dispatcher(make_spec("opencode"), tmp_path).start(inputs)
    await handle.wait()
    first = [ev.type async for ev in handle.events()]
    second = await asyncio.wait_for(_drain(handle), 5)
    assert first[-1] == "run.finished" and second == []


async def _drain(handle):
    return [ev async for ev in handle.events()]


async def test_resume_unknown_run_is_not_resumable(tmp_path, make_spec):
    with pytest.raises(NotResumable, match="no-such-run"):
        await dispatcher(make_spec("claude"), tmp_path).start_resume("no-such-run", "hi")


def test_home_cleanup_failure_is_a_warning(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))

    def boom(path):
        raise OSError("file in use")
    monkeypatch.setattr("phd.dispatcher.robust_rmtree", boom)
    d = dispatcher(make_spec("claude", resumable=False), tmp_path)
    r = d.spawn(inputs)
    assert r.status is RunStatus.SUCCEEDED and not r.resumable
    assert any("could not remove run home" in w and r.home_dir in w for w in r.warnings)
    assert (tmp_path / "runs" / r.run_id / "result.json").is_file()


async def test_stack_hash_pinned_at_plan_time(tmp_path, monkeypatch, make_spec, inputs):
    from phd.stack import Stack
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    monkeypatch.setenv("FAKE_SLEEP", "1")
    d = dispatcher(make_spec("claude"), tmp_path)
    handle = await d.start(inputs)

    def boom(self):
        raise OSError("stack moved")
    monkeypatch.setattr(Stack, "content_hash", boom)
    r = await handle.wait()
    assert r.status is RunStatus.SUCCEEDED and r.stack_hash.startswith("sha256:")
    resumed = await (await d.start_resume(r, "again")).wait()
    assert resumed.stack_hash == r.stack_hash


async def test_build_result_failure_still_yields_result(tmp_path, monkeypatch, make_spec, inputs):
    from phd.dispatcher import RunHandle

    def boom(self, *a, **k):
        raise ValueError("bad outcome")
    monkeypatch.setattr(RunHandle, "_build_result", boom)
    handle = await dispatcher(make_spec("claude"), tmp_path).start(inputs)
    r = await asyncio.wait_for(handle.wait(), 20)
    assert r.status is not RunStatus.SUCCEEDED and r.error.kind == "phd_internal"
    assert "run.finished" in [ev.type async for ev in handle.events()]


async def test_interrupted_run_kills_process_tree(tmp_path, monkeypatch, make_spec, inputs):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CHILD_PIDFILE", str(pidfile))
    monkeypatch.setenv("FAKE_SLEEP", "60")
    handle = await dispatcher(make_spec("claude"), tmp_path).start(inputs)
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.1)
    grandchild, child = int(pidfile.read_text()), handle._proc.proc.pid
    handle._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle._task
    assert wait_dead(child) and wait_dead(grandchild)


def test_timeout_covers_a_stalled_stdin_write(tmp_path, monkeypatch, make_spec, inputs):
    from phd import Inputs
    monkeypatch.setenv("FAKE_NO_STDIN", "1")
    monkeypatch.setenv("FAKE_SLEEP", "30")
    big = Inputs(brief=inputs.brief, rules=inputs.rules, msg="x" * (2 * 1024 * 1024))
    t0 = time.monotonic()
    r = dispatcher(make_spec("claude", timeout_s=2), tmp_path).spawn(big)
    assert r.status is RunStatus.TIMED_OUT and time.monotonic() - t0 < 15


def test_plan_repr_hides_secrets(tmp_path, make_spec, inputs):
    assert "test-oauth-token" not in repr(dispatcher(make_spec("claude"), tmp_path).plan(inputs))


def test_exit_zero_without_final_result_fails(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_EXIT", "0")
    r = dispatcher(make_spec("claude"), tmp_path).spawn(inputs)
    assert r.status is RunStatus.FAILED and r.error.kind == "no_result"


def test_crash_stderr_tail_is_scrubbed(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_EXIT", "3")
    monkeypatch.setenv("FAKE_STDERR", "boom token=test-oauth-token prefix=phd-echo end")
    r = dispatcher(make_spec("claude"), tmp_path).spawn(inputs)
    assert r.error.kind == "harness_crash" and "boom" in r.error.message and "<redacted>" in r.error.message
    text = (tmp_path / "runs" / r.run_id / "result.json").read_text(encoding="utf-8")
    assert "test-oauth-token" not in text and "phd-echo" not in text
