"""RunHandle error paths: failing sinks, internal errors mid-run, odd stdout lines, handles without a process."""

import asyncio
import json
import sys
import types

import pytest

from conftest import FAKE_HARNESS, STREAMS
from phd import Dispatcher, Inputs, RunStatus
from phd.execution import RunHandle

FAKE = [sys.executable, str(FAKE_HARNESS)]


class BadSink:
    async def put(self, result):
        raise OSError("disk full")


def dispatcher(spec, tmp_path, **kw):
    kw.setdefault("sinks", ())
    return Dispatcher(spec, runs_root=tmp_path / "runs", validate_model=False, allow_temp_runs_root=True,
                      executables={spec.harness: FAKE}, **kw)


async def test_a_sink_that_fails_is_recorded_not_raised(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    handle = await dispatcher(make_spec("claude"), tmp_path, sinks=[BadSink()]).start(inputs)
    result = await asyncio.wait_for(handle.wait(), 20)
    assert result.status is RunStatus.SUCCEEDED
    assert any("result sink BadSink failed: disk full" in w for w in result.warnings)


async def test_a_sink_that_fails_for_an_unresumable_session_is_recorded(tmp_path, make_spec):
    d = dispatcher(make_spec("claude"), tmp_path, sinks=[BadSink()])
    handle = await d.start(Inputs(msg="hi"), session_id="no-such-session")
    result = await handle.wait()
    assert result.error.kind == "session"
    assert any("result sink BadSink failed: disk full" in w for w in result.warnings)


async def test_an_internal_error_kills_the_harness_and_still_yields_a_result(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_SLEEP", "60")

    async def boom(self):
        await asyncio.sleep(0.3)  # the harness is up and sleeping
        raise RuntimeError("internal failure")
    monkeypatch.setattr(RunHandle, "_run_to_result", boom)
    handle = await dispatcher(make_spec("claude"), tmp_path).start(inputs)
    result = await asyncio.wait_for(handle.wait(), 20)
    assert result.error.kind == "phd_internal" and "internal failure" in result.error.message
    assert handle._proc.proc.returncode is not None  # the harness was killed and reaped
    assert not handle._d.ledger.root.joinpath(result.run_id, ".phd-session.lock").exists()  # lock released


async def test_a_failure_while_consuming_output_becomes_an_internal_error(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_SLEEP", "60")

    async def boom(self, *a, **k):
        raise ValueError("parser exploded")
    monkeypatch.setattr(RunHandle, "_consume", boom)
    handle = await dispatcher(make_spec("claude"), tmp_path).start(inputs)
    result = await asyncio.wait_for(handle.wait(), 20)
    assert result.status is not RunStatus.SUCCEEDED
    assert result.error.kind == "phd_internal" and result.error.message == "ValueError: parser exploded"
    assert handle._proc.killed  # the harness was stopped, not left sleeping
    assert "run.finished" in [ev.type async for ev in handle.events()]


async def test_blank_and_non_object_stdout_lines_are_skipped(tmp_path, monkeypatch, make_spec, inputs):
    lines = (STREAMS / "claude_ok.jsonl").read_text(encoding="utf-8").splitlines()
    noisy = tmp_path / "noisy.jsonl"
    noisy.write_text("\n".join(["", "   ", "[1, 2, 3]", "42", '"text"', *lines]) + "\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(noisy))
    handle = await dispatcher(make_spec("claude"), tmp_path).start(inputs)
    result = await asyncio.wait_for(handle.wait(), 20)
    assert result.status is RunStatus.SUCCEEDED
    events = [json.loads(line) for line in open(result.events_path, encoding="utf-8")]
    assert not [e for e in events if e["type"] == "log"]  # neither blanks nor bare JSON values are logged


async def test_a_process_less_handle_finishes_and_releases_nothing(tmp_path, make_spec, inputs):
    d = dispatcher(make_spec("claude"), tmp_path)
    m = d.plan(inputs)
    handle = RunHandle(d, m, None, "sha256:x")  # never launched: no process, no lock
    result = handle._minimal_result(RuntimeError("stub"))

    async def fake_run():
        return result
    handle._run_to_result = fake_run
    await handle._pump()
    assert handle._result is result and handle._queue.get_nowait() is None


async def test_kill_and_reap_survives_a_process_that_will_not_be_reaped(tmp_path, make_spec, inputs):
    killed = []

    class Stubborn:
        proc = types.SimpleNamespace(returncode=None)

        def kill_tree(self):
            killed.append(1)

        async def wait(self):
            raise OSError("cannot wait")

    d = dispatcher(make_spec("claude"), tmp_path)
    handle = RunHandle(d, d.plan(inputs), None, "sha256:x")
    handle._proc = Stubborn()
    await handle._kill_and_reap(timeout_s=1)
    assert killed == [1]


async def test_kill_and_reap_ignores_a_process_that_already_exited(tmp_path, make_spec, inputs):
    class Gone:
        proc = types.SimpleNamespace(returncode=0)

        def kill_tree(self):
            raise AssertionError("must not kill an exited process")

        async def wait(self):
            raise AssertionError("must not wait for an exited process")

    d = dispatcher(make_spec("claude"), tmp_path)
    handle = RunHandle(d, d.plan(inputs), None, "sha256:x")
    handle._proc = Gone()
    await handle._kill_and_reap()


async def test_a_cancelled_run_is_not_repaired(tmp_path, make_spec, inputs):
    d = dispatcher(make_spec("claude"), tmp_path)
    handle = RunHandle(d, d.plan(inputs), None, "sha256:x")
    handle._cancelled = True
    first = handle._minimal_result(RuntimeError("schema"))
    assert await handle._repair(None, first, None, None, deadline=1e18) is first
    assert first.warnings == []
