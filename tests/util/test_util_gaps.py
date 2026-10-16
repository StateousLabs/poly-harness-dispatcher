"""Error and OS-failure paths of the small `_util` helpers (process, filesystem, file lock)."""

import os
import subprocess
import types
from pathlib import Path

import pytest

from phd._util import filelock, fs
from phd._util import proc as proc_mod
from phd._util.filelock import FileLock, is_stale


# ---- proc.py ---------------------------------------------------------------------------------------------------

def _fake_os(**attrs):
    return types.SimpleNamespace(name="posix", getpid=os.getpid, **attrs)


@pytest.fixture
def sigkill(monkeypatch):
    """`signal.SIGKILL` does not exist on Windows; the POSIX branch of kill_tree needs it."""
    monkeypatch.setattr(proc_mod, "signal", types.SimpleNamespace(SIGKILL=9))
    return 9


def _raiser(exc):
    def fn(*a, **k):
        raise exc
    return fn


def test_pid_alive_posix_permission_error_means_alive(monkeypatch):
    monkeypatch.setattr(proc_mod, "os", _fake_os(kill=_raiser(PermissionError())))
    assert proc_mod.pid_alive(1234) is True


def test_pid_alive_posix_missing_process_is_dead(monkeypatch):
    monkeypatch.setattr(proc_mod, "os", _fake_os(kill=_raiser(ProcessLookupError())))
    assert proc_mod.pid_alive(1234) is False


def test_is_zombie_is_false_without_proc(monkeypatch):
    def no_open(*a, **k):
        raise FileNotFoundError("no /proc here")
    monkeypatch.setattr("builtins.open", no_open)
    assert proc_mod._is_zombie(1234) is False


class _FakeProc:
    pid = 4321

    def __init__(self, returncode=None, kill_error=None):
        self.returncode, self.kill_error, self.killed = returncode, kill_error, False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        if self.kill_error:
            raise self.kill_error


def test_kill_tree_does_nothing_for_an_exited_process(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _raiser(AssertionError("must not be called")))
    proc = _FakeProc(returncode=0)
    proc_mod.kill_tree(proc)
    assert not proc.killed


@pytest.mark.parametrize("error", [OSError("taskkill missing"), subprocess.TimeoutExpired("taskkill", 15)])
def test_kill_tree_windows_falls_back_to_kill_when_taskkill_fails(monkeypatch, error):
    monkeypatch.setattr(proc_mod, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(proc_mod.subprocess, "run", _raiser(error))
    proc = _FakeProc()
    proc_mod.kill_tree(proc)
    assert proc.killed


@pytest.mark.parametrize("error", [ProcessLookupError(), PermissionError()])
def test_kill_tree_posix_survives_a_vanished_group_and_still_kills_the_leader(monkeypatch, sigkill, error):
    monkeypatch.setattr(proc_mod, "os", _fake_os(killpg=_raiser(error)))
    proc = _FakeProc()
    proc_mod.kill_tree(proc)
    assert proc.killed


def test_kill_tree_posix_sigkills_the_group(monkeypatch, sigkill):
    calls = []
    monkeypatch.setattr(proc_mod, "os", _fake_os(killpg=lambda pid, sig: calls.append((pid, sig))))
    proc_mod.kill_tree(_FakeProc())
    assert calls == [(4321, sigkill)]


def test_kill_tree_ignores_a_process_that_died_before_the_final_kill(monkeypatch, sigkill):
    monkeypatch.setattr(proc_mod, "os", _fake_os(killpg=lambda pid, sig: None))
    proc = _FakeProc(kill_error=ProcessLookupError())
    proc_mod.kill_tree(proc)  # must not raise
    assert proc.killed


# ---- fs.py -----------------------------------------------------------------------------------------------------

def test_write_private_keeps_the_data_when_chmod_fails(tmp_path, monkeypatch):
    target = tmp_path / "sub" / "secret"
    monkeypatch.setattr(Path, "chmod", _raiser(PermissionError("denied")))
    fs.write_private(target, b"s3cret\r\n")
    assert target.read_bytes() == b"s3cret\r\n"


def test_robust_rmtree_retries_a_transient_failure(tmp_path, monkeypatch):
    target = tmp_path / "tree"
    target.mkdir()
    real, calls = fs.shutil.rmtree, []

    def flaky(path, **kw):
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError("sharing violation")
        real(path, **kw)

    monkeypatch.setattr(fs.shutil, "rmtree", flaky)
    monkeypatch.setattr(fs.time, "sleep", lambda s: None)
    fs.robust_rmtree(target)
    assert len(calls) == 3 and not target.exists()


def test_robust_rmtree_gives_up_after_the_last_attempt(tmp_path, monkeypatch):
    sleeps, calls = [], []

    def always(path, **kw):
        calls.append(path)
        raise PermissionError("locked")

    monkeypatch.setattr(fs.shutil, "rmtree", always)
    monkeypatch.setattr(fs.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError):
        fs.robust_rmtree(tmp_path, attempts=3)
    assert len(calls) == 3 and sleeps == [0.2, 0.4]  # backs off, and does not sleep after the final failure


def test_robust_rmtree_of_a_missing_directory_is_fine(tmp_path):
    fs.robust_rmtree(tmp_path / "nope")


def test_remove_path_removes_a_link_like_an_empty_directory_when_unlink_refuses(tmp_path, monkeypatch):
    path = tmp_path / "junction"
    path.mkdir()  # stands in for a Windows junction / directory symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == path)
    monkeypatch.setattr(Path, "unlink", _raiser(PermissionError("is a directory link")))
    fs.remove_path(path)
    assert not path.exists()


def test_remove_path_raises_when_the_path_survives(tmp_path, monkeypatch):
    target = tmp_path / "stubborn"
    target.write_text("x")
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: None)  # "succeeds" but deletes nothing
    with pytest.raises(OSError, match="could not remove"):
        fs.remove_path(target)
    assert target.exists()


def test_remove_path_makes_a_read_only_file_writable(tmp_path, monkeypatch):
    target = tmp_path / "ro"
    target.write_text("x")
    real_unlink, calls = Path.unlink, []

    def unlink(self, missing_ok=False):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("read-only")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    fs.remove_path(target)
    assert not target.exists() and len(calls) == 2


# ---- filelock.py -----------------------------------------------------------------------------------------------

def test_is_stale_is_false_for_a_lock_file_that_vanished(tmp_path):
    assert is_stale(tmp_path / "gone.lock") is False


def test_filelock_exit_without_enter_is_a_no_op(tmp_path):
    lock = FileLock(tmp_path / "x.lock")
    assert lock.__exit__(None, None, None) is None


def test_filelock_can_be_reacquired_after_a_double_exit(tmp_path):
    lock = FileLock(tmp_path / "x.lock", timeout_s=1)
    with lock:
        pass
    lock.__exit__(None, None, None)  # second exit: descriptor already released
    with FileLock(tmp_path / "x.lock", timeout_s=1):
        pass


# ---- platform branches, driven by fakes so both run everywhere -----------------------------------------------------

def test_pid_alive_posix_reports_a_live_non_zombie(monkeypatch):
    monkeypatch.setattr(proc_mod, "os", _fake_os(kill=lambda pid, sig: None))
    monkeypatch.setattr(proc_mod, "_is_zombie", lambda pid: False)
    assert proc_mod.pid_alive(1234) is True
    monkeypatch.setattr(proc_mod, "_is_zombie", lambda pid: True)
    assert proc_mod.pid_alive(1234) is False


@pytest.mark.parametrize("stat, zombie", [(b"42 (my (odd) name) Z 1 2 3", True), (b"42 (sleeper) S 1 2 3", False)])
def test_is_zombie_reads_the_state_after_the_last_parenthesis(monkeypatch, stat, zombie):
    import io
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.BytesIO(stat))
    assert proc_mod._is_zombie(42) is zombie


class _Kernel32:
    def __init__(self, handle, ok=1, exit_code=259):
        self.handle, self.ok, self.exit_code, self.closed = handle, ok, exit_code, []

    def OpenProcess(self, access, inherit, pid):
        return self.handle

    def GetExitCodeProcess(self, handle, code):
        code.value = self.exit_code
        return self.ok

    def CloseHandle(self, handle):
        self.closed.append(handle)


@pytest.mark.parametrize("kernel, alive", [
    (_Kernel32(handle=0), False),                    # no such process
    (_Kernel32(handle=7, exit_code=259), True),      # STILL_ACTIVE
    (_Kernel32(handle=7, exit_code=0), False),       # exited
    (_Kernel32(handle=7, ok=0), False),              # exit code unreadable
])
def test_pid_alive_windows_checks_the_exit_code(monkeypatch, kernel, alive):
    import ctypes
    monkeypatch.setattr(proc_mod, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=kernel), raising=False)
    monkeypatch.setattr(ctypes, "byref", lambda obj: obj)
    assert proc_mod.pid_alive(1234) is alive
    assert kernel.closed == ([7] if kernel.handle else [])  # the process handle is always closed


def test_robust_rmtree_makes_a_read_only_file_writable_and_retries(tmp_path, monkeypatch):
    victim = tmp_path / "ro.txt"
    victim.write_text("x")
    victim.chmod(0o444)

    def fake_rmtree(path, onexc):
        onexc(os.remove, str(victim), PermissionError("read-only"))  # what shutil reports for a protected file
    monkeypatch.setattr(fs.shutil, "rmtree", fake_rmtree)
    fs.robust_rmtree(tmp_path / "tree")
    assert not victim.exists()


def test_deep_merge_keeps_equal_scalars_quietly_and_records_only_real_conflicts():
    conflicts = []
    merged = fs.deep_merge({"a": 1, "b": 2, "c": {"d": 1}}, {"a": 1, "b": 3, "c": {"d": 2}}, conflicts=conflicts)
    assert merged == {"a": 1, "b": 2, "c": {"d": 1}} and conflicts == ["b", "c.d"]
    assert fs.deep_merge({"b": 2}, {"b": 3}) == {"b": 2}  # without a conflicts list the clash is silently ignored
