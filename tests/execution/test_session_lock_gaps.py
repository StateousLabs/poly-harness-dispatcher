"""Rare acquire / takeover outcomes of the per-home session lock."""

import json
import os
import subprocess
import sys

import pytest

from phd.errors import NotResumable
from phd.execution import session_lock as mod
from phd.execution.session_lock import LOCK_NAME, SessionLock


def dead_pid() -> int:
    return int(subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                              capture_output=True, text=True).stdout)


def test_acquire_gives_up_when_the_lock_keeps_appearing_and_vanishing(tmp_path, monkeypatch):
    def always_exists(path, flags, mode=0o777):
        raise FileExistsError()
    monkeypatch.setattr(os, "open", always_exists)
    monkeypatch.setattr(mod, "read_holder", lambda path: None)  # never readable, and the file is never there
    lock = SessionLock.sweeper(tmp_path)
    with pytest.raises(NotResumable, match=r"could not acquire .*\.phd-session\.lock"):
        lock.acquire("run home x")  # a sweeper does not wait, so the retries run out
    assert lock.run_id not in mod._HELD


def test_acquire_gives_up_when_a_stale_lock_cannot_be_removed(tmp_path, monkeypatch):
    (tmp_path / LOCK_NAME).write_text(json.dumps({"pid": dead_pid(), "run_id": "gone"}), encoding="utf-8")
    attempts = []
    monkeypatch.setattr(SessionLock, "_take_over", lambda self, what, holder: attempts.append(holder))
    lock = SessionLock(tmp_path, "run-a")
    with pytest.raises(NotResumable, match="could not acquire"):
        lock.acquire("session s")
    assert len(attempts) == 3 and not lock.held and "run-a" not in mod._HELD


@pytest.mark.parametrize("pid", ["123", None, 1.5])
def test_a_lock_whose_pid_is_not_an_integer_is_not_running(pid):
    assert mod._running({"pid": pid, "run_id": "x"}) is False


def test_a_lock_with_a_non_integer_pid_is_taken_over(tmp_path):
    (tmp_path / LOCK_NAME).write_text(json.dumps({"pid": "not-a-pid", "run_id": "old"}), encoding="utf-8")
    lock = SessionLock(tmp_path, "run-a").acquire("session s")
    assert json.loads((tmp_path / LOCK_NAME).read_text(encoding="utf-8"))["run_id"] == "run-a"
    lock.release()


def test_unlink_of_an_already_deleted_lock_is_fine(tmp_path):
    mod._unlink(tmp_path / "missing.lock")
