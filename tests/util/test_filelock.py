import json
import os
import subprocess
import sys
import time

import pytest

from phd._util.filelock import FileLock, is_stale, read_holder


def dead_pid():
    return int(subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                              capture_output=True, text=True).stdout)


def test_lock_and_release(tmp_path):
    path = tmp_path / ".lock"
    with FileLock(path):
        assert read_holder(path)["pid"] == os.getpid()
    assert not path.exists()


def test_times_out_while_held_by_live_process(tmp_path):
    path = tmp_path / ".lock"
    path.write_text(json.dumps({"pid": os.getpid()}))
    with pytest.raises(TimeoutError):
        with FileLock(path, timeout_s=0.3):
            pass


def test_dead_holder_is_taken_over(tmp_path):
    path = tmp_path / ".lock"
    path.write_text(json.dumps({"pid": dead_pid()}))
    assert is_stale(path)
    with FileLock(path, timeout_s=1):
        assert read_holder(path)["pid"] == os.getpid()


def test_unreadable_lock_is_stale_only_after_grace(tmp_path):
    path = tmp_path / ".lock"
    path.write_text("")
    assert not is_stale(path, grace_s=60)
    old = time.time() - 120
    os.utime(path, (old, old))
    assert is_stale(path, grace_s=60)


def _flaky(real, fails, matches):
    state = {"left": fails}

    def wrapper(*args, **kw):
        if matches(args) and state["left"] > 0:
            state["left"] -= 1
            raise PermissionError("sharing violation")
        return real(*args, **kw)
    return wrapper, state


def test_release_retries_a_sharing_violation(tmp_path, monkeypatch):
    path = tmp_path / ".lock"
    wrapper, state = _flaky(os.unlink, 2, lambda a: str(a[0]) == str(path))
    with FileLock(path):
        monkeypatch.setattr(os, "unlink", wrapper)
    assert state["left"] == 0 and not path.exists()


def test_a_persistent_sharing_violation_is_reraised(tmp_path, monkeypatch):
    from phd._util.filelock import retry_sharing

    def always():
        raise PermissionError("x")
    monkeypatch.setattr("phd._util.filelock.time.sleep", lambda s: None)
    with pytest.raises(PermissionError):
        retry_sharing(always)


def test_release_leaves_a_foreign_lock_alone(tmp_path):
    path = tmp_path / ".lock"
    with FileLock(path):
        path.write_text(json.dumps({"pid": os.getpid(), "token": "someone-else"}))
    assert read_holder(path)["token"] == "someone-else"


def test_takeover_of_a_fresh_lock_puts_it_back(tmp_path, monkeypatch):
    # is_stale is patched to claim "stale" once for a lock that is in fact live: it must be restored
    path = tmp_path / ".lock"
    path.write_text(json.dumps({"pid": os.getpid(), "token": "live"}))
    lock = FileLock(path, timeout_s=0.2)
    lock.token = "t"
    assert lock._take_over() is False
    assert read_holder(path)["token"] == "live" and not list(tmp_path.glob("*.stale"))


RACER = r"""
import os, sys, time
from pathlib import Path
from phd._util.filelock import FileLock
lock, marker, violations, go = map(Path, sys.argv[1:5])
while not go.exists():
    time.sleep(0.001)
for _ in range(3):
    with FileLock(lock, timeout_s=60):
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            with open(violations, "a") as f:
                f.write("overlap\n")
        else:
            os.close(fd)
            time.sleep(0.02)
            os.unlink(marker)
"""


def test_racing_takeovers_of_one_stale_lock_stay_exclusive(tmp_path):
    lock, marker, violations, go = (tmp_path / n for n in (".lock", "marker", "violations", "go"))
    lock.write_text(json.dumps({"pid": dead_pid()}))
    procs = [subprocess.Popen([sys.executable, "-c", RACER, str(lock), str(marker), str(violations), str(go)])
             for _ in range(4)]
    go.write_text("go")
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0, 0]
    assert not violations.exists() and not lock.exists()
