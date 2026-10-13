"""`reaper_main.py` run directly, as a child process the way `ReaperGuard` launches it. Running it here (with the
test process's environment, which carries coverage's variables) is what lets subprocess coverage see the script."""

import os
import subprocess
import sys
import time

import pytest

from phd.execution.lifetime import reaper

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the reaper is POSIX-only")

SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


def start_reaper(pgid: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-I", str(reaper.SCRIPT), str(pgid)], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)


@pytest.fixture
def group():
    proc = subprocess.Popen(SLEEPER, start_new_session=True)
    yield proc
    proc.kill()
    proc.wait()


def test_eof_on_stdin_kills_the_group(group):
    r = start_reaper(group.pid)
    time.sleep(0.5)
    r.stdin.close()  # the caller "died"
    assert r.wait(timeout=10) == 0
    assert group.wait(timeout=10) < 0  # SIGKILL


def test_exit_line_leaves_the_group_alone(group):
    r = start_reaper(group.pid)
    r.stdin.write("exit\n")
    r.stdin.flush()
    assert r.wait(timeout=10) == 0
    assert group.poll() is None
    r.stdin.close()


def test_exits_by_itself_once_the_group_is_gone(group):
    r = start_reaper(group.pid)
    group.kill()
    group.wait()
    assert r.wait(timeout=10) == 0  # the 1 s poll notices the dead group
    r.stdin.close()


def test_eof_for_an_already_gone_group_is_harmless():
    gone = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    gone.wait()
    r = start_reaper(gone.pid)
    r.stdin.close()
    assert r.wait(timeout=10) == 0
