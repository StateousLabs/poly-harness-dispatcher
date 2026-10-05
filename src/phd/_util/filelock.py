"""A blocking inter-process lock file; a lock left by a dead process is taken over."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .proc import pid_alive

UNREADABLE_GRACE_S = 2.0


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
    """`with FileLock(path):` — waits up to `timeout_s` for the lock (TimeoutError after that)."""

    def __init__(self, path: Path, *, timeout_s: float = 30.0, poll_s: float = 0.05):
        self.path, self.timeout_s, self.poll_s = Path(path), timeout_s, poll_s

    def __enter__(self) -> FileLock:
        deadline = time.monotonic() + self.timeout_s
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if is_stale(self.path):
                    _unlink(self.path)
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out after {self.timeout_s}s waiting for {self.path}") from None
                time.sleep(self.poll_s)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid()}, f)
            return self

    def __exit__(self, *exc) -> None:
        _unlink(self.path)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
