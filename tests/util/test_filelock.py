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
        assert read_holder(path) == {"pid": os.getpid()}
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
