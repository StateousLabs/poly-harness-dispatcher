import json
import os
import subprocess
import sys
import time

import pytest

from phd._util.filelock import FileLock, is_stale, read_holder, retry_sharing


def dead_pid():
    return int(subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                              capture_output=True, text=True).stdout)


HOLDER = r"""
import os, sys, time
from pathlib import Path
from phd._util.filelock import FileLock
lock, ready, hold, mode = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
lk = FileLock(lock, timeout_s=60)
lk.__enter__()
ready.write_text("held")
time.sleep(hold)
if mode == "crash":
    os._exit(0)
lk.__exit__(None, None, None)
"""

CONTENDER = r"""
import sys, time
from pathlib import Path
from phd._util.filelock import FileLock
lock, log, go = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
while not go.exists():
    time.sleep(0.001)
with FileLock(lock, timeout_s=60):
    start = time.time()
    time.sleep(0.3)
    end = time.time()
Path(log).write_text(f"{start} {end}")
"""


def _hold(tmp_path, hold_s, mode="release"):
    lock, ready = tmp_path / ".lock", tmp_path / "ready"
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock), str(ready), str(hold_s), mode])
    deadline = time.monotonic() + 30
    while not ready.exists():
        assert time.monotonic() < deadline and proc.poll() is None
        time.sleep(0.01)
    return lock, proc


def test_lock_file_survives_release_and_can_be_reacquired(tmp_path):
    path = tmp_path / "sub" / ".lock"
    with FileLock(path):
        assert path.exists()
    assert path.exists()
    with FileLock(path, timeout_s=1):
        pass


def test_times_out_while_another_process_holds_the_lock(tmp_path):
    lock, proc = _hold(tmp_path, 3)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="timed out after 0.3s waiting for"):
            with FileLock(lock, timeout_s=0.3):
                pass
        assert time.monotonic() - started < 2.5
    finally:
        proc.wait(timeout=30)
    with FileLock(lock, timeout_s=5):  # released by the holder on exit
        pass


def test_a_second_lock_in_the_same_process_blocks(tmp_path):
    path = tmp_path / ".lock"
    with FileLock(path):
        with pytest.raises(TimeoutError):
            with FileLock(path, timeout_s=0.2):
                pass
    with FileLock(path, timeout_s=1):
        pass


def test_lock_is_released_when_the_holder_crashes(tmp_path):
    lock, proc = _hold(tmp_path, 0.2, mode="crash")
    proc.wait(timeout=30)
    started = time.monotonic()
    with FileLock(lock, timeout_s=2):
        pass
    assert time.monotonic() - started < 2


def test_two_processes_never_hold_the_lock_together(tmp_path):
    lock, go = tmp_path / ".lock", tmp_path / "go"
    logs = [tmp_path / "a.log", tmp_path / "b.log"]
    procs = [subprocess.Popen([sys.executable, "-c", CONTENDER, str(lock), str(log), str(go)]) for log in logs]
    go.write_text("go")
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    (s1, e1), (s2, e2) = sorted(tuple(map(float, log.read_text().split())) for log in logs)
    assert e1 <= s2


def test_retry_sharing_retries_then_succeeds_or_reraises(monkeypatch):
    monkeypatch.setattr("phd._util.filelock.time.sleep", lambda s: None)
    calls = {"n": 0}

    def twice_then_ok():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("sharing violation")
        return "ok"
    assert retry_sharing(twice_then_ok) == "ok" and calls["n"] == 3

    def always():
        raise PermissionError("x")
    with pytest.raises(PermissionError):
        retry_sharing(always)


def test_read_holder_and_is_stale_for_session_locks(tmp_path):
    path = tmp_path / ".lock"
    path.write_text(json.dumps({"pid": os.getpid()}))
    assert read_holder(path) == {"pid": os.getpid()} and not is_stale(path)
    path.write_text(json.dumps({"pid": dead_pid()}))
    assert is_stale(path)
    path.write_text("")
    assert not is_stale(path, grace_s=60)
    old = time.time() - 120
    os.utime(path, (old, old))
    assert is_stale(path, grace_s=60)
