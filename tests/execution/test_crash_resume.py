"""Resuming a session whose run died before writing result.json (the caller was killed mid-run)."""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import FAKE_HARNESS, FIXTURES, STREAMS
from helpers import wait_dead
from phd import Dispatcher, Inputs, RunStatus
from phd.errors import NotResumable
from test_lifetime import kill_pid, read_pid

CRASH_CALLER = Path(__file__).parents[1] / "fakes" / "crash_caller.py"
FAKE = [sys.executable, str(FAKE_HARNESS)]


def dispatcher(spec, tmp_path):
    return Dispatcher(spec, runs_root=tmp_path / "runs", validate_model=False, allow_temp_runs_root=True,
                      executables={spec.harness: FAKE})


def read_line(stream, timeout: float) -> str:
    """One line from `stream`, failing (not hanging) when none arrives within `timeout` seconds."""
    box: list[str] = []
    reader = threading.Thread(target=lambda: box.append(stream.readline().strip()), daemon=True)
    reader.start()
    reader.join(timeout)
    assert box, f"no line within {timeout}s"
    return box[0]


def crash_a_run(tmp_path, workdir, *flags) -> Path:
    """Run a caller whose harness reports session `sess-crash` and hangs; kill the caller. Returns the run dir."""
    init = tmp_path / "init.jsonl"
    init.write_text('{"type":"system","subtype":"init","session_id":"sess-crash","claude_code_version":"x"}\n',
                    encoding="utf-8")
    env = os.environ | {"FAKE_STREAM": str(init), "FAKE_SLEEP": "120", "FAKE_PIDFILE": str(tmp_path / "harness.pid")}
    caller = subprocess.Popen([sys.executable, str(CRASH_CALLER), str(FIXTURES / "stack"), str(workdir),
                               str(tmp_path / "runs"), str(FAKE_HARNESS), *flags], stdout=subprocess.PIPE, text=True,
                              env=env)
    harness = None
    try:
        assert read_line(caller.stdout, 30) == "READY"
        harness = read_pid(tmp_path / "harness.pid")
        caller.kill()
        caller.wait(timeout=10)
        assert wait_dead(harness, 10), "harness outlived its caller"
    finally:
        if caller.poll() is None:
            caller.kill()
        if harness is not None:
            kill_pid(harness)
    (run_dir,) = (tmp_path / "runs").iterdir()
    return run_dir


def test_a_session_whose_only_run_crashed_can_be_resumed(tmp_path, monkeypatch, make_spec, workdir):
    run_dir = crash_a_run(tmp_path, workdir)
    assert (run_dir / "session.json").is_file() and not (run_dir / "result.json").exists()
    record = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
    assert record["session_id"] == "sess-crash" and record["harness"] == "claude" and record["resumable"] is True
    assert record["home"] == str(run_dir / "home") and record["cwd"] == str(workdir)
    echo = tmp_path / "echo.jsonl"
    monkeypatch.setenv("FAKE_ECHO_FILE", str(echo))
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    for var in ("FAKE_SLEEP", "FAKE_PIDFILE"):
        monkeypatch.delenv(var, raising=False)
    r = dispatcher(make_spec("claude"), tmp_path).spawn(Inputs(msg="again"), session_id="sess-crash")
    assert r.status is RunStatus.SUCCEEDED and r.resumed_from == run_dir.name
    assert r.home_dir == str(run_dir / "home")
    argv = json.loads(echo.read_text(encoding="utf-8").splitlines()[-1])["argv"]
    assert argv[-2:] == ["--resume", "sess-crash"]


def test_a_crashed_run_that_was_not_resumable_is_refused(tmp_path, make_spec, workdir):
    run_dir = crash_a_run(tmp_path, workdir, "not-resumable")
    before = sorted((tmp_path / "runs").iterdir())
    r = dispatcher(make_spec("claude"), tmp_path).spawn(Inputs(msg="again"), session_id="sess-crash")
    assert r.status is RunStatus.FAILED and r.error.kind == "session"
    assert r.error.message == (f"session 'sess-crash': its run {run_dir.name} was not resumable (resumable=False) "
                            "and did not finish")
    assert sorted((tmp_path / "runs").iterdir()) == sorted([*before, Path(r.run_dir)])


async def test_a_session_held_by_a_live_run_is_busy(tmp_path, monkeypatch, make_spec):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    monkeypatch.setenv("FAKE_SLEEP", "30")
    d = dispatcher(make_spec("claude"), tmp_path)
    live = await d.start(Inputs(msg="x"))
    try:
        deadline = time.monotonic() + 30
        while live.session_id is None:
            assert time.monotonic() < deadline, "the session id was never reported"
            await asyncio.sleep(0.05)
        with pytest.raises(NotResumable, match="is busy with run"):
            await d.start(Inputs(msg="again"), session_id=live.session_id)
    finally:
        await live.cancel()


def test_the_session_record_is_written_for_fresh_and_resumed_runs(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    spec = make_spec("claude")
    d = dispatcher(spec, tmp_path)
    first = d.spawn(inputs)
    second = d.spawn(Inputs(msg="more"), session_id=first.session_id)
    for r in (first, second):
        rec = json.loads((Path(r.run_dir) / "session.json").read_text(encoding="utf-8"))
        assert rec["session_id"] == "sess-claude-1" and rec["harness"] == "claude"
        assert rec["cwd"] == str(spec.cwd) and rec["home"] == first.home_dir
        assert rec["stack"] == spec.stack.ref and rec["resumable"] is True
        assert isinstance(rec["started_at"], str) and rec["started_at"]
    assert not list(Path(first.run_dir).glob("*.tmp"))


def test_a_failed_record_write_is_a_warning(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(d.ledger, "write_session", boom)
    r = d.spawn(inputs)
    assert r.status is RunStatus.SUCCEEDED
    assert any("session record" in w and "disk full" in w for w in r.warnings)


async def test_an_internal_error_after_the_session_started_keeps_the_session(tmp_path, monkeypatch, make_spec, inputs):
    from phd.execution import RunHandle

    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))

    def boom(self, *a, **k):
        raise ValueError("bad outcome")
    monkeypatch.setattr(RunHandle, "_build_result", boom)
    d = dispatcher(make_spec("claude"), tmp_path)
    first = await (await d.start(inputs)).wait()
    assert first.error.kind == "phd_internal" and first.session_id == "sess-claude-1" and first.resumable
    monkeypatch.undo()
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    again = await (await d.start(Inputs(msg="again"), session_id="sess-claude-1")).wait()
    assert again.status is RunStatus.SUCCEEDED and again.resumed_from == first.run_id
