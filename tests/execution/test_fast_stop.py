"""Leaving `async with d.run(...)` returns within ~10 s even when the harness ignores every stop signal (B2)."""
import asyncio
import sys
import time

import pytest

from conftest import FAKE_HARNESS
from helpers import wait_dead
from phd import Dispatcher, Inputs, RunStatus

FAKE = [sys.executable, str(FAKE_HARNESS)]
BOUND = 10.0


@pytest.fixture
def stubborn(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IGNORE_SIGNALS", "1")
    monkeypatch.setenv("FAKE_SLEEP", "60")
    monkeypatch.setenv("FAKE_PIDFILE", str(tmp_path / "harness.pid"))
    monkeypatch.setenv("FAKE_CHILD_PIDFILE", str(tmp_path / "child.pid"))
    return tmp_path / "harness.pid", tmp_path / "child.pid"


async def _pids(handle, harness_file, child_file):
    for _ in range(200):
        if all(f.exists() and f.read_text() for f in (harness_file, child_file)) and handle.materialized:
            break
        await asyncio.sleep(0.05)
    return int(harness_file.read_text()), int(child_file.read_text())


async def _scenario(tmp_path, make_spec, inputs, files, interrupt):
    d = Dispatcher(make_spec("claude"), runs_root=tmp_path / "runs", validate_model=False,
                   allow_temp_runs_root=True, executables={"claude": FAKE})
    seen: dict = {}

    async def body():
        async with d.run(inputs) as handle:
            seen["handle"] = handle
            seen["pids"] = await _pids(handle, *files)
            seen["ready"] = True
            if interrupt == "error":
                seen["t0"] = time.monotonic()
                raise ValueError("boom")
            await asyncio.sleep(120)

    task = asyncio.create_task(body())
    while not seen.get("ready"):
        await asyncio.sleep(0.05)
    if interrupt == "cancel":
        seen["t0"] = time.monotonic()
        task.cancel()
    expected = asyncio.CancelledError if interrupt == "cancel" else ValueError
    with pytest.raises(expected):
        await asyncio.wait_for(task, BOUND + 5)
    elapsed = time.monotonic() - seen["t0"]
    print(f"\nfast stop ({interrupt}): {elapsed:.2f}s")
    assert elapsed < BOUND
    harness, child = seen["pids"]
    assert wait_dead(harness, 2) and wait_dead(child, 2), "harness or grandchild survived"
    results = d.ledger.list()
    assert [r.status for r in results] == [RunStatus.CANCELLED]
    return elapsed


async def test_cancelled_task_stops_fast(tmp_path, make_spec, inputs, stubborn):
    await _scenario(tmp_path, make_spec, inputs, stubborn, "cancel")


async def test_exception_in_block_stops_fast(tmp_path, make_spec, inputs, stubborn):
    await _scenario(tmp_path, make_spec, inputs, stubborn, "error")
