"""Dispatcher cleanup and refusal paths: failed dry runs, failed launches, cancelled or failed planning."""

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import FAKE_HARNESS
from phd import Dispatcher
from phd.errors import NotResumable
from phd.execution import dispatcher as dispatcher_mod
from phd.execution.handle import RunHandle
from phd.materialize import MaterializedHome

FAKE = [sys.executable, str(FAKE_HARNESS)]


def dispatcher(spec, tmp_path):
    return Dispatcher(spec, runs_root=tmp_path / "runs", validate_model=False, allow_temp_runs_root=True,
                      executables={spec.harness: FAKE})


def stub_home(tmp_path) -> MaterializedHome:
    run_dir = tmp_path / "runs" / "stub-run"
    (run_dir / "home").mkdir(parents=True)
    return MaterializedHome(run_id="stub-run", run_dir=run_dir, home=run_dir / "home", argv=[], env={}, stdin="",
                            warnings=[])


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_a_dry_run_that_fails_has_no_lock_to_release(tmp_path, make_spec, inputs, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("materialize failed")
    monkeypatch.setattr(dispatcher_mod, "materialize", boom)
    with pytest.raises(RuntimeError, match="materialize failed"):
        dispatcher(make_spec("claude"), tmp_path).plan(inputs)


async def test_a_launch_failure_without_a_lock_still_drops_secrets(tmp_path, make_spec, inputs, monkeypatch):
    d = dispatcher(make_spec("claude"), tmp_path)
    m = stub_home(tmp_path)
    dropped = []
    monkeypatch.setattr(d, "_plan", lambda inputs, session_id, dry_run=False: (m, "sha256:x", None, None, 0.0))
    monkeypatch.setattr(d, "_drop_secrets", dropped.append)

    async def refuse(*a, **k):
        raise FileNotFoundError("harness vanished")
    monkeypatch.setattr(RunHandle, "launch", refuse)
    with pytest.raises(FileNotFoundError):
        await d.start(inputs)
    assert dropped == [m.home]


async def test_a_launch_failure_releases_the_lock(tmp_path, make_spec, inputs, monkeypatch):
    d = dispatcher(make_spec("claude"), tmp_path)
    released = []

    class Lock:
        def release(self):
            released.append(1)

    m = stub_home(tmp_path)
    monkeypatch.setattr(d, "_plan", lambda inputs, session_id, dry_run=False: (m, "sha256:x", None, Lock(), 0.0))

    async def refuse(*a, **k):
        raise FileNotFoundError("harness vanished")
    monkeypatch.setattr(RunHandle, "launch", refuse)
    with pytest.raises(FileNotFoundError):
        await d.start(inputs)
    assert released == [1]


async def test_a_cancelled_start_cleans_a_fresh_run_without_a_lock(tmp_path, make_spec, inputs, monkeypatch):
    d = dispatcher(make_spec("claude"), tmp_path)
    m = stub_home(tmp_path)
    gate, dropped = threading.Event(), []

    def slow_plan(inputs, session_id, dry_run=False):
        gate.wait(10)
        return m, "sha256:x", None, None, 0.0
    monkeypatch.setattr(d, "_plan", slow_plan)
    monkeypatch.setattr(d, "_drop_secrets", dropped.append)

    task = asyncio.create_task(d.start(inputs))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    assert await asyncio.to_thread(wait_for, lambda: not m.run_dir.exists())  # a fresh run's directory is removed
    assert dropped == [m.home]


async def test_a_cancelled_resume_keeps_the_sessions_shared_home(tmp_path, make_spec, inputs, monkeypatch):
    d = dispatcher(make_spec("claude"), tmp_path)
    m = stub_home(tmp_path)
    gate, dropped, released = threading.Event(), [], []

    class Lock:
        def release(self):
            released.append(1)

    def slow_plan(inputs, session_id, dry_run=False):
        gate.wait(10)
        return m, "sha256:x", "prev-run", Lock(), 0.0
    monkeypatch.setattr(d, "_plan", slow_plan)
    monkeypatch.setattr(d, "_drop_secrets", dropped.append)

    task = asyncio.create_task(d.start(inputs, session_id="sess-1"))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    assert await asyncio.to_thread(wait_for, lambda: released)
    assert dropped == [m.home] and m.run_dir.exists()  # a resumed turn's home is never removed


async def test_a_cancelled_start_whose_planning_failed_has_nothing_to_clean(tmp_path, make_spec, inputs, monkeypatch):
    d = dispatcher(make_spec("claude"), tmp_path)
    gate, cleaned = threading.Event(), []

    def failing_plan(inputs, session_id, dry_run=False):
        gate.wait(10)
        raise RuntimeError("plan failed")
    monkeypatch.setattr(d, "_plan", failing_plan)
    monkeypatch.setattr(d, "_drop_secrets", cleaned.append)

    task = asyncio.create_task(d.start(inputs))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await asyncio.sleep(0.5)  # the done-callback has run by now; a failed plan produced nothing to clean
    assert cleaned == []


def test_a_run_home_that_is_a_link_is_refused(tmp_path, make_spec, monkeypatch):
    d = dispatcher(make_spec("claude"), tmp_path)
    home = d.ledger.root / "r1" / "home"
    home.mkdir(parents=True)
    # a real symlink/junction needs privileges on Windows: report this one path as a link instead
    monkeypatch.setattr(dispatcher_mod, "is_link", lambda p: Path(p) == home)
    with pytest.raises(NotResumable, match="is a symlink or junction"):
        d._check_home_contained("sess-1", home)


def test_planning_refuses_a_stack_that_was_removed(tmp_path, make_spec, inputs):
    import shutil
    from phd import Stack
    from phd.errors import StackNotFound
    copy = tmp_path / "stack-copy"
    shutil.copytree(make_spec("claude").stack.root, copy)
    d = dispatcher(make_spec("claude", stack=Stack.load(copy)), tmp_path)
    shutil.rmtree(copy)
    with pytest.raises(StackNotFound, match="no longer exists"):
        d.plan(inputs)
