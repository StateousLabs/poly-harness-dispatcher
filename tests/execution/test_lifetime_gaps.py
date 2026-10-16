"""Failure paths of the lifetime guards (POSIX reaper, Windows job object), driven with fakes so they run anywhere."""

import ctypes
import subprocess
import threading
import types

import pytest

from phd.execution.lifetime import reaper, windows_job
from phd.execution.lifetime.reaper import ReaperGuard
from phd.execution.lifetime.windows_job import JobGuard


# ---- ReaperGuard ------------------------------------------------------------------------------------------------

def test_reaper_that_cannot_start_leaves_a_warning_not_an_error(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("no interpreter")
    monkeypatch.setattr(reaper.subprocess, "Popen", boom)
    guard = ReaperGuard()
    guard.attach(1234)
    assert guard.warning.startswith("orphan protection unavailable: reaper did not start (FileNotFoundError")
    assert "no interpreter" in guard.warning
    guard.release()  # nothing to release; must not raise


class _Stdin:
    def __init__(self, write_error=None, close_error=None):
        self.write_error, self.close_error, self.written, self.closed = write_error, close_error, [], False

    def write(self, data):
        if self.write_error:
            raise self.write_error
        self.written.append(data)

    def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


class _Proc:
    def __init__(self, stdin):
        self.stdin, self.waited = stdin, threading.Event()

    def wait(self, timeout=None):
        self.waited.set()
        return 0


def test_release_tells_the_reaper_to_exit_and_reaps_it_in_the_background():
    guard, proc = ReaperGuard(), _Proc(_Stdin())
    guard._proc = proc
    guard.release()
    assert proc.stdin.written == [b"exit\n"] and proc.stdin.closed
    assert proc.waited.wait(5)
    guard.release()  # idempotent: the process was handed off


def test_release_survives_a_reaper_that_already_left():
    guard = ReaperGuard()
    proc = _Proc(_Stdin(write_error=BrokenPipeError("gone"), close_error=OSError("closed")))
    guard._proc = proc
    guard.release()  # neither the failed write nor the failed close may raise
    assert proc.stdin.closed and proc.waited.wait(5)


def test_a_reaper_that_will_not_exit_is_killed():
    class Stuck:
        def __init__(self):
            self.killed, self.calls = False, 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("reaper", timeout)
            return 0

        def kill(self):
            self.killed = True

    proc = Stuck()
    reaper._reap(proc)
    assert proc.killed and proc.calls == 2


def test_a_reaper_that_dies_while_being_killed_is_still_reaped():
    class Racy:
        calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("reaper", timeout)

        def kill(self):
            raise ProcessLookupError()

    proc = Racy()
    reaper._reap(proc)  # must not raise
    assert proc.calls == 2


# ---- JobGuard._assign with a fake kernel32 -----------------------------------------------------------------------

class _Kernel:
    def __init__(self, *, job=11, set_ok=True, proc=22, assign_ok=True):
        self.job, self.set_ok, self.proc, self.assign_ok = job, set_ok, proc, assign_ok
        self.closed, self.terminated = [], []

    def CreateJobObjectW(self, *a):
        return self.job

    def SetInformationJobObject(self, *a):
        return self.set_ok

    def OpenProcess(self, *a):
        return self.proc

    def AssignProcessToJobObject(self, *a):
        return self.assign_ok

    def CloseHandle(self, handle):
        self.closed.append(handle)

    def TerminateJobObject(self, job, code):
        self.terminated.append(job)
        return 1


@pytest.fixture
def kernel(monkeypatch):
    """Install a fake kernel32 (and the Windows-only ctypes error helpers, absent on POSIX)."""
    def install(**kw):
        k = _Kernel(**kw)
        monkeypatch.setattr(windows_job, "_kernel32", lambda: k)
        monkeypatch.setattr(ctypes, "WinError", lambda code=None: OSError("win32 failure"), raising=False)
        monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
        return k
    return install


def test_assign_returns_the_job_and_closes_the_process_handle(kernel):
    k = kernel()
    assert JobGuard()._assign(99) == 11
    assert k.closed == [22]


def test_assign_fails_when_the_job_cannot_be_created(kernel):
    k = kernel(job=0)
    with pytest.raises(OSError, match="win32 failure"):
        JobGuard()._assign(99)
    assert k.closed == []


def test_assign_closes_the_job_when_limits_cannot_be_set(kernel):
    k = kernel(set_ok=False)
    with pytest.raises(OSError):
        JobGuard()._assign(99)
    assert k.closed == [11]


def test_assign_closes_the_job_when_the_process_cannot_be_opened(kernel):
    k = kernel(proc=0)
    with pytest.raises(OSError):
        JobGuard()._assign(99)
    assert k.closed == [11]  # no process handle to close


def test_assign_closes_both_handles_when_assignment_is_refused(kernel):
    k = kernel(assign_ok=False)
    with pytest.raises(OSError):
        JobGuard()._assign(99)
    assert sorted(k.closed) == [11, 22]


def test_attach_records_a_warning_and_still_resumes_when_the_job_fails(kernel, monkeypatch):
    kernel(set_ok=False)
    resumed = []
    monkeypatch.setattr(windows_job, "resume", resumed.append)
    guard = JobGuard()
    guard.attach(99)
    assert guard.warning.startswith("orphan protection unavailable: OSError")
    assert resumed == [99] and guard.kill() is False  # no job: the caller falls back to its own kill


def test_attach_terminates_a_harness_that_cannot_be_resumed(kernel, monkeypatch):
    kernel()
    terminated = []

    def refuse(pid):
        raise OSError("NtResumeProcess failed")
    monkeypatch.setattr(windows_job, "resume", refuse)
    monkeypatch.setattr(windows_job, "_terminate", terminated.append)
    guard = JobGuard()
    guard.attach(99)
    assert terminated == [99] and "NtResumeProcess failed" in guard.warning


def test_kill_terminates_the_job_and_release_closes_it_once(kernel, monkeypatch):
    k = kernel()
    monkeypatch.setattr(windows_job, "resume", lambda pid: None)
    guard = JobGuard()
    guard.attach(99)
    assert guard.warning is None and guard.kill() is True and k.terminated == [11]
    guard.release()
    guard.release()
    assert k.closed.count(11) == 1


# ---- new_guard ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("os_name, guard_type", [("nt", JobGuard), ("posix", ReaperGuard)])
def test_new_guard_picks_the_guard_for_the_os(monkeypatch, os_name, guard_type):
    from phd.execution import lifetime
    monkeypatch.setattr(lifetime, "os", types.SimpleNamespace(name=os_name))
    assert type(lifetime.new_guard()) is guard_type
