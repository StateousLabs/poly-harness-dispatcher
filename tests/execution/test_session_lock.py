import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import FAKE_HARNESS, STREAMS
from phd import Dispatcher, Inputs, RunStatus
from phd.errors import NotResumable
from phd.execution.session_lock import LOCK_NAME, SessionLock

FAKE = [sys.executable, str(FAKE_HARNESS)]


def dispatcher(spec, tmp_path, **kw):
    kw.setdefault("runs_root", tmp_path / "runs")
    kw.setdefault("validate_model", False)
    kw.setdefault("allow_temp_runs_root", True)
    return Dispatcher(spec, executables={spec.harness: FAKE}, **kw)


def _snapshot(home: Path) -> dict:
    return {str(p.relative_to(home)): p.stat().st_mtime_ns for p in home.rglob("*") if p.is_file()}


def _run_dirs(tmp_path) -> list[str]:
    return sorted(p.name for p in (tmp_path / "runs").iterdir())


def _dead_pid() -> int:
    out = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    return int(out.stdout)


async def _finished(d, inputs, **kw):
    return await (await d.start(inputs, **kw)).wait()


async def test_a_running_turn_blocks_another_turn_of_the_session(tmp_path, monkeypatch, make_spec):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = await _finished(d, Inputs(msg="first"))
    home = Path(first.home_dir)
    assert not (home / LOCK_NAME).exists()

    monkeypatch.setenv("FAKE_SLEEP", "30")
    busy = await d.start(Inputs(msg="second"), session_id=first.session_id)
    holder = json.loads((home / LOCK_NAME).read_text(encoding="utf-8"))
    assert holder["run_id"] == busy.run_id and holder["pid"] == os.getpid()

    before, runs_before = _snapshot(home), _run_dirs(tmp_path)
    with pytest.raises(NotResumable, match=f"busy with run {busy.run_id}"):
        await d.start(Inputs(msg="third"), session_id=first.session_id)
    assert _snapshot(home) == before and _run_dirs(tmp_path) == runs_before  # nothing written or touched

    await busy.cancel()
    assert not (home / LOCK_NAME).exists()
    monkeypatch.setenv("FAKE_SLEEP", "0")
    again = await _finished(d, Inputs(msg="fourth"), session_id=first.session_id)
    assert again.status is RunStatus.SUCCEEDED and not (home / LOCK_NAME).exists()


async def test_a_fresh_run_holds_its_home_lock_until_it_ends(tmp_path, monkeypatch, make_spec):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    handle = await dispatcher(make_spec("claude"), tmp_path).start(Inputs(msg="x"))
    lock = handle.materialized.home / LOCK_NAME
    assert json.loads(lock.read_text(encoding="utf-8"))["run_id"] == handle.run_id
    result = await handle.cancel()
    assert result.status is RunStatus.CANCELLED and not lock.exists()


@pytest.mark.parametrize("env, status, spec_kw", [({"FAKE_SLEEP": "30"}, RunStatus.TIMED_OUT, {"timeout_s": 2}),
                                                  ({"FAKE_EXIT": "3"}, RunStatus.FAILED, {})])
def test_lock_is_released_on_timeout_and_crash(env, status, spec_kw, tmp_path, monkeypatch, make_spec):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    r = dispatcher(make_spec("claude", **spec_kw), tmp_path).spawn(Inputs(msg="x"))
    assert r.status is status and not (Path(r.home_dir) / LOCK_NAME).exists()


@pytest.mark.parametrize("stale", ["dead-pid", "corrupt", "same-process-finished"])
def test_stale_locks_are_taken_over(stale, tmp_path, monkeypatch, make_spec):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(Inputs(msg="first"))
    lock = Path(first.home_dir) / LOCK_NAME
    content = {"dead-pid": json.dumps({"pid": _dead_pid(), "run_id": "ghost", "started_at": "x"}),
               "corrupt": "{not json",
               "same-process-finished": json.dumps({"pid": os.getpid(), "run_id": "crashed-earlier",
                                                     "started_at": "x"})}[stale]
    lock.write_text(content, encoding="utf-8")
    if stale == "corrupt":
        old = time.time() - 120
        os.utime(lock, (old, old))
    again = d.spawn(Inputs(msg="again"), session_id=first.session_id)
    assert again.status is RunStatus.SUCCEEDED and not lock.exists()


async def test_runs_on_different_sessions_do_not_block_each_other(tmp_path, monkeypatch, make_spec):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    d = dispatcher(make_spec("claude"), tmp_path)
    a, b = await d.start(Inputs(msg="a")), await d.start(Inputs(msg="b"))
    assert (a.materialized.home / LOCK_NAME).exists() and (b.materialized.home / LOCK_NAME).exists()
    await a.cancel()
    await b.cancel()


async def test_plan_ignores_the_lock(tmp_path, monkeypatch, make_spec):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = await _finished(d, Inputs(msg="first"))
    monkeypatch.setenv("FAKE_SLEEP", "30")
    busy = await d.start(Inputs(msg="second"), session_id=first.session_id)
    lock = Path(first.home_dir) / LOCK_NAME
    preview = d.plan(Inputs(msg="preview"), session_id=first.session_id)
    assert preview.home != Path(first.home_dir) and not (preview.home / LOCK_NAME).exists()
    assert json.loads(lock.read_text(encoding="utf-8"))["run_id"] == busy.run_id
    await busy.cancel()


def test_release_only_removes_our_own_lock(tmp_path):
    lock = SessionLock(tmp_path, "run-a").acquire("session s")
    (tmp_path / LOCK_NAME).write_text(json.dumps({"pid": 1, "run_id": "run-b"}), encoding="utf-8")
    lock.release()
    assert json.loads((tmp_path / LOCK_NAME).read_text(encoding="utf-8"))["run_id"] == "run-b"
    lock.release()  # idempotent


def test_acquire_refuses_a_live_holder(tmp_path):
    first = SessionLock(tmp_path, "run-a").acquire("session s")
    with pytest.raises(NotResumable, match="session s is busy with run run-a"):
        SessionLock(tmp_path, "run-b").acquire("session s")
    first.release()
    SessionLock(tmp_path, "run-b").acquire("session s").release()
    assert not (tmp_path / LOCK_NAME).exists()


def test_busy_within_grace_for_a_fresh_unreadable_lock(tmp_path):
    (tmp_path / LOCK_NAME).write_text("")
    with pytest.raises(NotResumable, match="lock file is being written"):
        SessionLock(tmp_path, "run-a").acquire("session s")


def test_lock_released_between_the_two_looks_is_retried(tmp_path, monkeypatch):
    # the create hits an existing lock, but by the time we read it the file is gone: retry, do not report busy
    import phd.execution.session_lock as mod
    state = {"first": True}
    real_open = os.open

    def open_once(path, flags, mode=0o777):
        if state["first"]:
            state["first"] = False
            raise FileExistsError()
        return real_open(path, flags, mode)
    monkeypatch.setattr(os, "open", open_once)
    monkeypatch.setattr(mod, "read_holder", lambda path: None)
    lock = SessionLock(tmp_path, "run-a").acquire("session s")
    assert lock.held and not state["first"]
    monkeypatch.undo()
    lock.release()


def _write_stale(home: Path) -> None:
    (home / LOCK_NAME).write_text(json.dumps({"pid": _dead_pid(), "run_id": "run-dead"}), encoding="utf-8")


def test_takeover_of_a_stale_lock_is_serialized(tmp_path, monkeypatch):
    # A has seen the stale lock and is about to unlink it when B also tries to take it over: only one may win
    import phd.execution.session_lock as mod
    _write_stale(tmp_path)
    monkeypatch.setattr(mod, "TAKEOVER_TIMEOUT_S", 0.3)
    a, b = SessionLock(tmp_path, "run-a"), SessionLock(tmp_path, "run-b")
    real_unlink, state = mod._unlink, {"b": None}

    def unlink_after_b_tries(path):
        if state["b"] is None:  # first unlink: A's, with B racing it
            state["b"] = "tried"
            try:
                b.acquire("session s")
                state["b"] = "won"
            except NotResumable:
                state["b"] = "busy"
        real_unlink(path)
    monkeypatch.setattr(mod, "_unlink", unlink_after_b_tries)
    a.acquire("session s")
    assert state["b"] == "busy" and a.held and not b.held
    assert json.loads((tmp_path / LOCK_NAME).read_text(encoding="utf-8"))["run_id"] == "run-a"
    a.release()


def test_a_takeover_that_lost_the_race_does_not_unlink_the_winners_lock(tmp_path, monkeypatch):
    # B takes the stale lock over between A's look and A's turn at the takeover step: A must leave B's lock alone
    import phd.execution.session_lock as mod
    _write_stale(tmp_path)
    a, b = SessionLock(tmp_path, "run-a"), SessionLock(tmp_path, "run-b")
    real_lock, state = mod.FileLock, {"done": False}

    class RacingLock(real_lock):
        def __enter__(self):
            if not state["done"]:
                state["done"] = True
                b.acquire("session s")  # B completes its whole takeover first
            return super().__enter__()
    monkeypatch.setattr(mod, "FileLock", RacingLock)
    with pytest.raises(NotResumable, match="busy with run run-b"):
        a.acquire("session s")
    assert b.held and not a.held
    assert json.loads((tmp_path / LOCK_NAME).read_text(encoding="utf-8"))["run_id"] == "run-b"
    b.release()


def test_free_home_does_not_touch_the_takeover_lock(tmp_path):
    SessionLock(tmp_path, "run-a").acquire("session s").release()
    assert not (tmp_path / (LOCK_NAME + ".takeover")).exists()
