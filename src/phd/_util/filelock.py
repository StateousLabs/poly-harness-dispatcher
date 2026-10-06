"""A blocking inter-process lock (OS file lock) plus the lock-file helpers the session lock uses."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, TypeVar

from .proc import pid_alive

UNREADABLE_GRACE_S = 2.0
RETRY_TRIES = 20
RETRY_MAX_DELAY_S = 0.1

T = TypeVar("T")


def retry_sharing(fn: Callable[[], T], *, tries: int = RETRY_TRIES) -> T:
    """Call `fn`, retrying on PermissionError with a short backoff (about 1 s in all).

    On Windows a process that has a file open for reading blocks unlink / rename / replace of it with
    PermissionError; the reader is gone in a moment, so we wait and try again, then re-raise."""
    for attempt in range(tries):
        try:
            return fn()
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(min(0.01 * (attempt + 1), RETRY_MAX_DELAY_S))
    raise AssertionError("unreachable")  # pragma: no cover


def read_holder(path: Path) -> dict | None:
    """The lock file's JSON object, or None if missing, unreadable or not an object."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_stale(path: Path, *, grace_s: float = UNREADABLE_GRACE_S) -> bool:
    """True if the lock's process is gone, or the file has been unreadable for longer than `grace_s`
    (a writer may sit between creating and writing the file)."""
    holder = read_holder(path)
    if holder is None:
        try:
            return time.time() - Path(path).stat().st_mtime > grace_s
        except FileNotFoundError:
            return False
    pid = holder.get("pid")
    return not (isinstance(pid, int) and pid_alive(pid))


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, 0)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:  # BlockingIOError while another descriptor holds it
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class FileLock:
    """`with FileLock(path):` — waits up to `timeout_s` for the lock (TimeoutError after that).

    An OS lock on the file (`msvcrt.locking` on Windows, `flock` elsewhere): the kernel releases it when
    the holding process dies, so there is no stale-lock takeover. The lock is per open descriptor, so a
    second FileLock on the same path in the same process blocks too. Nothing is written into the file
    (a locked byte range would block readers on Windows) and the file is never deleted."""

    def __init__(self, path: Path, *, timeout_s: float = 30.0, poll_s: float = 0.05):
        self.path, self.timeout_s, self.poll_s = Path(path), timeout_s, poll_s
        self._fd: int | None = None

    def __enter__(self) -> FileLock:
        deadline = time.monotonic() + self.timeout_s
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out after {self.timeout_s}s waiting for {self.path}")
                time.sleep(self.poll_s)
        except BaseException:  # timeout, Ctrl+C, anything: never leak the descriptor
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *exc) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)

