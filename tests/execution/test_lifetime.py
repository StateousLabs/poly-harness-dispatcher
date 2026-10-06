import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import FAKE_HARNESS, FIXTURES, STREAMS
from helpers import wait_dead
from phd import Dispatcher, Inputs, RunStatus
from phd._util.proc import pid_alive
from phd.execution import lifetime
from phd.execution.process import HarnessProcess, child_env

CALLER = Path(__file__).parents[1] / "fakes" / "caller.py"
FAKE = [sys.executable, str(FAKE_HARNESS)]


def read_pid(path: Path, timeout: float = 15.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"{path} never written")


def test_harness_and_grandchild_die_with_their_caller(tmp_path, workdir):
    env = os.environ | {"FAKE_PIDFILE": str(tmp_path / "harness.pid"),
                        "FAKE_CHILD_PIDFILE": str(tmp_path / "child.pid"), "FAKE_SLEEP": "120"}
    caller = subprocess.Popen([sys.executable, str(CALLER), str(FIXTURES / "stack"), str(workdir),
                               str(tmp_path / "runs"), str(FAKE_HARNESS)], stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert caller.stdout.readline().strip() == "READY"
        harness, child = read_pid(tmp_path / "harness.pid"), read_pid(tmp_path / "child.pid")
        assert pid_alive(harness) and pid_alive(child)
        caller.kill()  # TerminateProcess / SIGKILL: no Python cleanup runs in the caller
        caller.wait(timeout=10)
        assert wait_dead(harness, 10), "harness outlived its caller"
        assert wait_dead(child, 10), "grandchild outlived its caller"
    finally:
        if caller.poll() is None:
            caller.kill()


async def test_normal_finish_kills_nothing_extra(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    p = HarnessProcess(FAKE, env=child_env({}), cwd=tmp_path, stdin_text="x", stderr_path=tmp_path / "e.log")
    await p.start()
    assert p.guard is not None and p.guard.warning is None
    _ = [line async for line in p.lines()]
    assert await p.wait() == 0 and not p.killed
    p.close()
    p.close()  # idempotent


@pytest.mark.skipif(os.name != "nt", reason="closing the job ends orphans only on Windows")
async def test_an_orphaned_grandchild_does_not_outlive_the_run_on_windows(tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CHILD_PIDFILE", str(pidfile))  # harness exits at once, grandchild keeps sleeping
    p = HarnessProcess(FAKE, env=child_env({}), cwd=tmp_path, stdin_text="x", stderr_path=tmp_path / "e.log")
    await p.start()
    child = await asyncio.to_thread(read_pid, pidfile)
    await p.wait()  # the harness is gone; wait() closes the job, which ends the orphan
    assert await asyncio.to_thread(wait_dead, child, 10)


@pytest.mark.skipif(os.name != "nt", reason="Windows job semantics")
async def test_kill_tree_terminates_the_job(tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CHILD_PIDFILE", str(pidfile))
    monkeypatch.setenv("FAKE_SLEEP", "60")
    p = HarnessProcess(FAKE, env=child_env({}), cwd=tmp_path, stdin_text="x", stderr_path=tmp_path / "e.log")
    await p.start()
    child = await asyncio.to_thread(read_pid, pidfile)
    p.kill_tree()
    assert p.killed and await asyncio.wait_for(p.wait(), 15) is not None
    assert await asyncio.to_thread(wait_dead, child, 10)


def test_setup_failure_is_a_warning_not_an_error(tmp_path, monkeypatch, make_spec):
    class Broken:
        warning = None

        def attach(self, pid):
            self.warning = "orphan protection unavailable: test"
            if os.name == "nt":
                from phd.execution.lifetime.windows_job import resume
                resume(pid)

        def kill(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(lifetime, "new_guard", lambda: Broken())
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = Dispatcher(make_spec("claude"), runs_root=tmp_path / "runs", validate_model=False,
                   allow_temp_runs_root=True, executables={"claude": FAKE})
    r = d.spawn(Inputs(msg="x"))
    assert r.status is RunStatus.SUCCEEDED and "orphan protection unavailable: test" in r.warnings


async def test_concurrent_runs_have_separate_guards(tmp_path, monkeypatch, make_spec):  # Review Focus 2
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    monkeypatch.setenv("FAKE_SLEEP", "30")
    d = Dispatcher(make_spec("claude"), runs_root=tmp_path / "runs", validate_model=False,
                   allow_temp_runs_root=True, executables={"claude": FAKE})
    a, b = await d.start(Inputs(msg="a")), await d.start(Inputs(msg="b"))
    assert (await a.cancel()).status is RunStatus.CANCELLED
    await asyncio.sleep(1)
    assert not b.done
    assert (await b.cancel()).status is RunStatus.CANCELLED
