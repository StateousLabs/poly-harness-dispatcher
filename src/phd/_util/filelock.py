"""A blocking inter-process lock file; a lock left by a dead process is taken over."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
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


class FileLock:
    """`with FileLock(path):` — waits up to `timeout_s` for the lock (TimeoutError after that).

    Each acquire writes a unique token into the lock file; release removes the file only if it still
    holds our token. A stale lock is taken over by *renaming* it to a name unique to this acquire
    (only one process can win that rename), never by a blind unlink that could delete a fresh lock."""

    def __init__(self, path: Path, *, timeout_s: float = 30.0, poll_s: float = 0.05):
        self.path, self.timeout_s, self.poll_s = Path(path), timeout_s, poll_s
        self.token = ""

    def __enter__(self) -> FileLock:
        deadline = time.monotonic() + self.timeout_s
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.token = uuid.uuid4().hex
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if is_stale(self.path) and self._take_over():
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out after {self.timeout_s}s waiting for {self.path}") from None
                time.sleep(self.poll_s)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "token": self.token,
                           "started_at": datetime.now(timezone.utc).isoformat()}, f)
            return self

    def __exit__(self, *exc) -> None:
        holder = read_holder(self.path)
        if holder is not None and holder.get("token") == self.token:
            _unlink(self.path)

    def _take_over(self) -> bool:
        """Claim the stale lock by renaming it aside. True if the caller should retry the create now.

        After winning the rename the claimed file is re-read: still stale -> delete it. Not stale (a fresh
        lock appeared between our check and the rename) -> put it back; if the path is taken again, drop the
        claimed file only when its holder's pid is dead, else leave it and keep polling."""
        claimed = self.path.with_name(f"{self.path.name}.{self.token}.stale")
        try:
            retry_sharing(lambda: os.rename(self.path, claimed))
        except OSError:  # gone already, or another process won the rename (or still blocked): poll again
            return False
        if is_stale(claimed):
            _unlink(claimed)
            return True
        if not self.path.exists():
            try:
                os.rename(claimed, self.path)
                return False
            except OSError:
                pass
        holder = read_holder(claimed)
        pid = holder.get("pid") if holder else None
        if isinstance(pid, int) and not pid_alive(pid):
            _unlink(claimed)
        return False


def _unlink(path: Path) -> None:
    try:
        retry_sharing(lambda: os.unlink(path))
    except FileNotFoundError:
        pass
