"""Error and OS-failure paths of the launcher, driven with fakes where a real failure cannot be provoked."""

import asyncio
import os
import signal
import sys
import threading
import types

import pytest

from conftest import FAKE_HARNESS
from phd.execution import process as launcher
from phd.execution.process import HarnessProcess, child_env


def make(tmp_path, stdin="hello"):
    return HarnessProcess([sys.executable, str(FAKE_HARNESS)], env=child_env({}), cwd=tmp_path,
                          stdin_text=stdin, stderr_path=tmp_path / "stderr.log")


# ---- start(): a guard that cannot attach -------------------------------------------------------------------------

async def test_start_reaps_the_harness_even_if_waiting_for_it_fails(tmp_path, monkeypatch):
    class BadGuard:
        warning = None
        released = False

        def attach(self, pid):
            raise RuntimeError("no protection")

        def release(self):
            self.released = True

    guard = BadGuard()
    monkeypatch.setattr(launcher.lifetime, "new_guard", lambda: guard)
    monkeypatch.setenv("FAKE_SLEEP", "60")
    real_wait_for = asyncio.wait_for

    async def failing_wait_for(coro, timeout):
        await real_wait_for(coro, 5)  # let the (killed) harness really exit, then fail anyway
        raise OSError("reap failed")

    monkeypatch.setattr(launcher, "asyncio", types.SimpleNamespace(**{**vars(asyncio), "wait_for": failing_wait_for}))
    p = make(tmp_path)
    with pytest.raises(RuntimeError, match="no protection"):  # the original failure wins over the reap failure
        await p.start()
    assert guard.released and p.proc.returncode is not None


# ---- _posix_kill_group -------------------------------------------------------------------------------------------

class FakeProc:
    pid = 777
    returncode = None

    def __init__(self):
        self.killed = 0

    def kill(self):
        self.killed += 1


@pytest.fixture
def killpg(monkeypatch):
    """Install a controllable `os.killpg` / `signal.SIGKILL` (neither exists on Windows)."""
    def install(error=None):
        calls = []

        def fake(pid, sig):
            calls.append((pid, sig))
            if error is not None:
                raise error
        monkeypatch.setattr(os, "killpg", fake, raising=False)
        monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
        return calls
    return install


def posix_handle(tmp_path):
    p = make(tmp_path)
    p.proc = FakeProc()
    return p


def test_a_group_kill_that_works_does_not_touch_the_leader(tmp_path, killpg):
    calls = killpg()
    p = posix_handle(tmp_path)
    p._posix_kill_group()
    assert calls == [(777, 9)] and p.proc.killed == 0


def test_a_vanished_group_is_not_an_error_and_needs_no_second_kill(tmp_path, killpg):
    killpg(ProcessLookupError())
    p = posix_handle(tmp_path)
    p._posix_kill_group()
    assert p.proc.killed == 0


@pytest.mark.parametrize("error", [PermissionError("EPERM"), OSError("EINVAL")])
def test_any_other_group_kill_failure_falls_back_to_killing_the_leader(tmp_path, killpg, error):
    killpg(error)
    p = posix_handle(tmp_path)
    p._posix_kill_group()
    assert p.proc.killed == 1


# ---- _write_stdin ------------------------------------------------------------------------------------------------

class FakeStdin:
    def __init__(self, drain_error=None, close_error=None):
        self.drain_error, self.close_error, self.closed = drain_error, close_error, False

    def write(self, data):
        self.data = data

    async def drain(self):
        if self.drain_error:
            raise self.drain_error

    def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


async def test_a_harness_that_closed_stdin_early_is_not_an_error(tmp_path):
    p = make(tmp_path)
    p.proc = types.SimpleNamespace(stdin=FakeStdin(drain_error=BrokenPipeError()))
    await p._write_stdin()
    assert p.proc.stdin.closed and p.proc.stdin.data == b"hello"


async def test_a_stdin_that_cannot_be_closed_is_not_an_error(tmp_path):
    p = make(tmp_path)
    p.proc = types.SimpleNamespace(stdin=FakeStdin(close_error=ConnectionResetError()))
    await p._write_stdin()
    assert p.proc.stdin.closed


# ---- wait() ------------------------------------------------------------------------------------------------------

async def test_wait_cancels_a_stdin_write_that_can_never_finish(tmp_path):
    p = make(tmp_path)
    p.proc = types.SimpleNamespace(returncode=5)
    p._stdin_task = asyncio.create_task(asyncio.sleep(60))
    assert await p.wait() == 5
    assert p._stdin_task.cancelled()


async def test_wait_without_background_tasks_just_returns_the_code(tmp_path):
    p = make(tmp_path)
    p.proc = types.SimpleNamespace(returncode=0)
    assert await p.wait() == 0  # never started: no stdin or stderr task to settle


async def test_kill_tree_before_start_is_a_no_op(tmp_path):
    p = make(tmp_path)
    p.kill_tree()
    assert p.killed is False


# ---- _taskkill_fallback ------------------------------------------------------------------------------------------

class KillableProc:
    pid = 4242

    def __init__(self, returncode=None, kill_error=None):
        self.returncode, self.kill_error, self.kills = returncode, kill_error, 0
        self.killed = threading.Event()

    def kill(self):
        self.kills += 1
        self.killed.set()
        if self.kill_error:
            raise self.kill_error


class FakeTaskkill:
    def __init__(self, code):
        self.code = code

    def wait(self):
        return self.code


def fake_popen(monkeypatch, *, code=0, error=None):
    launched = []

    def popen(argv, **kw):
        launched.append(argv)
        if error:
            raise error
        return FakeTaskkill(code)
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    return launched


def join_reaper():
    for t in threading.enumerate():
        if t.name == "phd-taskkill":
            t.join(5)


def test_taskkill_that_cannot_start_kills_the_process_directly(monkeypatch):
    launched = fake_popen(monkeypatch, error=FileNotFoundError("no taskkill"))
    proc = KillableProc()
    HarnessProcess._taskkill_fallback(proc)
    assert launched and proc.kills == 1


def test_direct_kill_after_a_failed_taskkill_start_tolerates_a_dead_process(monkeypatch):
    fake_popen(monkeypatch, error=OSError("no taskkill"))
    proc = KillableProc(kill_error=ProcessLookupError())
    HarnessProcess._taskkill_fallback(proc)  # must not raise
    assert proc.kills == 1


def test_no_direct_kill_when_the_process_already_exited(monkeypatch):
    fake_popen(monkeypatch, error=OSError("no taskkill"))
    proc = KillableProc(returncode=0)
    HarnessProcess._taskkill_fallback(proc)
    assert proc.kills == 0


def test_a_successful_taskkill_needs_no_fallback(monkeypatch):
    launched = fake_popen(monkeypatch, code=0)
    proc = KillableProc()
    HarnessProcess._taskkill_fallback(proc)
    join_reaper()
    assert launched[0][:3] == ["taskkill", "/PID", "4242"] and proc.kills == 0


def test_a_failed_taskkill_without_a_loop_kills_directly_from_the_thread(monkeypatch):
    fake_popen(monkeypatch, code=128)
    proc = KillableProc()
    HarnessProcess._taskkill_fallback(proc)  # no running loop in a sync test
    assert proc.killed.wait(5)


async def test_a_failed_taskkill_hands_the_direct_kill_to_the_loop_thread(monkeypatch):
    fake_popen(monkeypatch, code=1)
    proc = KillableProc()
    threads = []
    real_kill = proc.kill

    def kill():
        threads.append(threading.current_thread())
        real_kill()
    proc.kill = kill
    HarnessProcess._taskkill_fallback(proc)
    for _ in range(100):
        if proc.kills:
            break
        await asyncio.sleep(0.05)
    assert proc.kills == 1 and threads == [threading.current_thread()]


def test_a_failed_taskkill_after_the_loop_closed_is_ignored(monkeypatch):
    fake_popen(monkeypatch, code=1)
    loop = asyncio.new_event_loop()
    loop.close()
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    proc = KillableProc()
    HarnessProcess._taskkill_fallback(proc)
    join_reaper()
    assert proc.kills == 0  # the closed loop cannot run it, and nothing blew up in the thread


# ---- kill_tree / _kill_unguarded per OS (the other OS's branch, via a faked os.name) -------------------------------

class StubGuard:
    def __init__(self, kills):
        self.kills = kills

    def kill(self):
        return self.kills


def fake_os(monkeypatch, name):
    monkeypatch.setattr(launcher, "os", types.SimpleNamespace(name=name, SEEK_END=os.SEEK_END))


def test_a_job_that_kills_the_tree_needs_no_fallback(tmp_path, monkeypatch):
    p = make(tmp_path)
    p.proc, p.guard = FakeProc(), StubGuard(True)
    monkeypatch.setattr(p, "_posix_kill_group", lambda: pytest.fail("job kill must be enough"))
    p.kill_tree()
    assert p.killed is True


def test_windows_without_a_job_falls_back_to_taskkill(tmp_path, monkeypatch):
    fake_os(monkeypatch, "nt")
    fallback = []
    monkeypatch.setattr(HarnessProcess, "_taskkill_fallback", staticmethod(fallback.append))
    p = make(tmp_path)
    p.proc, p.guard = FakeProc(), StubGuard(False)
    p.kill_tree()
    assert p.killed is True and fallback == [p.proc]


def test_windows_does_not_taskkill_a_process_that_already_exited(tmp_path, monkeypatch):
    fake_os(monkeypatch, "nt")
    monkeypatch.setattr(HarnessProcess, "_taskkill_fallback", staticmethod(lambda proc: pytest.fail("exited")))
    p = make(tmp_path)
    p.proc = FakeProc()
    p.proc.returncode = 0
    p.kill_tree()
    assert p.killed is False


def test_posix_kill_tree_kills_the_process_group(tmp_path, monkeypatch):
    fake_os(monkeypatch, "posix")
    p = make(tmp_path)
    p.proc, p.guard = FakeProc(), StubGuard(False)
    calls = []
    monkeypatch.setattr(p, "_posix_kill_group", lambda: calls.append(1))
    p.kill_tree()
    assert p.killed is True and calls == [1]


def test_unguarded_kill_per_os(tmp_path, monkeypatch):
    fake_os(monkeypatch, "nt")
    p = make(tmp_path)
    p.proc = FakeProc()
    p._kill_unguarded()  # Windows: the one (suspended) process is killed directly
    assert p.proc.killed == 1
    fake_os(monkeypatch, "posix")
    calls = []
    monkeypatch.setattr(p, "_posix_kill_group", lambda: calls.append(1))
    p._kill_unguarded()  # POSIX: its process group
    assert calls == [1] and p.proc.killed == 1


def test_unguarded_kill_on_windows_tolerates_a_dead_process(tmp_path, monkeypatch):
    fake_os(monkeypatch, "nt")

    class Dead(FakeProc):
        def kill(self):
            raise PermissionError("access denied")
    p = make(tmp_path)
    p.proc = Dead()
    p._kill_unguarded()  # must not raise
