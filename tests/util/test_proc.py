import os
import subprocess
import sys
import time

import pytest

from phd._util.proc import pid_alive


def test_pid_alive_true_for_this_process_and_false_for_nonsense():
    assert pid_alive(os.getpid())
    assert not pid_alive(0) and not pid_alive(-1)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="zombie processes and /proc are Linux-specific")
def test_pid_alive_is_false_for_an_unreaped_zombie():
    child = subprocess.Popen([sys.executable, "-c", "pass"])  # exits at once; never waited on until `finally`
    pid = child.pid
    try:
        # Exited but not waited on: os.kill(pid, 0) still succeeds, /proc says state Z.
        deadline = time.monotonic() + 5
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(pid, 0)  # still a process-table entry, i.e. genuinely a zombie
        assert not pid_alive(pid)
    finally:
        child.wait()
    assert not pid_alive(pid)
