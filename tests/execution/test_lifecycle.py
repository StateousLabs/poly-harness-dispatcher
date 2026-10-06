import asyncio
import sys
import time

import pytest

from conftest import FAKE_HARNESS, STREAMS
from phd import Dispatcher, Inputs, RunStatus

FAKE = [sys.executable, str(FAKE_HARNESS)]


def dispatcher(spec, tmp_path):
    return Dispatcher(spec, runs_root=tmp_path / "runs", validate_model=False, allow_temp_runs_root=True,
                      executables={spec.harness: FAKE})


@pytest.fixture
def slow(monkeypatch):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    monkeypatch.setenv("FAKE_SLEEP", "30")


async def test_run_block_that_waits_finishes_normally(tmp_path, monkeypatch, make_spec):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    async with dispatcher(make_spec("claude"), tmp_path).run(Inputs(msg="x")) as h:
        r = await h.wait()
    assert r.status is RunStatus.SUCCEEDED and h.done


async def test_leaving_the_block_without_wait_cancels(tmp_path, slow, make_spec):
    async with dispatcher(make_spec("claude"), tmp_path).run(Inputs(msg="x")) as h:
        pass
    assert h.done and (await h.wait()).status is RunStatus.CANCELLED


async def test_exception_in_the_block_cancels_and_reraises(tmp_path, slow, make_spec):
    with pytest.raises(RuntimeError, match="boom"):
        async with dispatcher(make_spec("claude"), tmp_path).run(Inputs(msg="x")) as h:
            raise RuntimeError("boom")
    assert (await h.wait()).status is RunStatus.CANCELLED


async def test_outer_cancellation_while_waiting_cancels_run(tmp_path, slow, make_spec):
    handles = []

    async def body():
        async with dispatcher(make_spec("claude"), tmp_path).run(Inputs(msg="x")) as h:
            handles.append(h)
            await h.wait()

    task = asyncio.create_task(body())
    while not handles:
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handles[0].done and (await handles[0].wait()).status is RunStatus.CANCELLED


async def test_outer_cancellation_during_events_cancels_run(tmp_path, slow, make_spec):  # Review Focus 1
    handles = []

    async def body():
        async with dispatcher(make_spec("claude"), tmp_path).run(Inputs(msg="x")) as h:
            handles.append(h)
            async for _ in h.events():
                pass

    task = asyncio.create_task(body())
    while not handles:
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    r = await handles[0].wait()
    assert r.status is RunStatus.CANCELLED and handles[0]._d.ledger.get(r.run_id).status is RunStatus.CANCELLED


@pytest.mark.parametrize("harness,stream,sid", [
    ("claude", "claude_ok.jsonl", "sess-claude-1"),
    ("codex", "codex_ok.jsonl", "01a0f55a-d4f3-7d43-943f-8708f4dfa0cf"),
    ("opencode", "opencode_ok.jsonl", "ses_abc"),
])
async def test_session_started_is_emitted_once(tmp_path, monkeypatch, make_spec, harness, stream, sid):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / stream))
    h = await dispatcher(make_spec(harness), tmp_path).start(Inputs(msg="x"))
    events = [e async for e in h.events()]
    r = await h.wait()
    types = [e.type for e in events]
    assert types.count("session.started") == 1
    assert types.index("run.started") < types.index("session.started") < types.index("run.finished")
    assert events[types.index("session.started")].data == {"session_id": sid}
    assert h.session_id == sid == r.session_id


async def test_resumed_turn_announces_its_session_at_once(tmp_path, monkeypatch, make_spec):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = await (await d.start(Inputs(msg="x"))).wait()
    h = await d.start(Inputs(msg="again"), session_id=first.session_id)
    assert h.session_id == first.session_id
    events = [e async for e in h.events()]
    assert [e.type for e in events][:2] == ["run.started", "session.started"]
    await h.wait()


def test_idle_limit_stops_a_silent_run(tmp_path, slow, make_spec):
    t0 = time.monotonic()
    r = dispatcher(make_spec("claude", idle_timeout_s=1), tmp_path).spawn(Inputs(msg="x"))
    assert r.status is RunStatus.TIMED_OUT and r.error.kind == "idle_timeout"
    assert r.error.message == "no output for idle_timeout_s=1" and time.monotonic() - t0 < 20


def test_steady_output_never_trips_the_idle_limit(tmp_path, monkeypatch, make_spec):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    monkeypatch.setenv("FAKE_LINE_DELAY", "0.4")
    r = dispatcher(make_spec("claude", idle_timeout_s=3), tmp_path).spawn(Inputs(msg="x"))
    assert r.status is RunStatus.SUCCEEDED
