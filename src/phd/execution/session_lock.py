"""Per-home lock: two turns must never use (and rewrite) the same harness session at once.

Every run that uses a home holds `<home>/.phd-session.lock` while it runs: a fresh run on its new
home, a resumed turn on the session's shared home (taken before the home is re-materialized). The
file records `{"pid", "run_id", "started_at"}`. A lock whose process is gone, or whose file cannot be
read, is stale and is taken over. Several runs can live in one Python process (async), so for a lock
written by this process the in-process registry `_HELD` decides whether its run is still going.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .._util.proc import pid_alive
from ..errors import NotResumable

LOCK_NAME = ".phd-session.lock"
_HELD: set[str] = set()  # run_ids that hold a lock in this process


class SessionLock:
    """The lock on one run home, held by one run. `acquire` raises `NotResumable` when busy."""

    def __init__(self, home: Path, run_id: str):
        self.path = Path(home) / LOCK_NAME
        self.run_id = run_id
        self.held = False

    def acquire(self, what: str) -> SessionLock:
        """Take the lock or raise `NotResumable("<what> is busy with run …")`. `what` names the session."""
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                holder = _read(self.path)
                if holder is not None and _running(holder):
                    raise NotResumable(f"{what} is busy with run {holder.get('run_id')} (pid {holder.get('pid')})") from None
                if _read(self.path) == holder:  # still the stale lock we looked at: take it over
                    _unlink(self.path)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "run_id": self.run_id,
                           "started_at": datetime.now(timezone.utc).isoformat()}, f)
            self.held = True
            _HELD.add(self.run_id)
            return self
        raise NotResumable(f"{what}: could not acquire {self.path}")

    def release(self) -> None:
        """Drop the lock if this run still holds it. Safe to call more than once."""
        if not self.held:
            return
        self.held = False
        _HELD.discard(self.run_id)
        holder = _read(self.path)
        if holder is not None and holder.get("run_id") == self.run_id:
            _unlink(self.path)


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _running(holder: dict) -> bool:
    pid = holder.get("pid")
    if not isinstance(pid, int):
        return False
    if pid == os.getpid():
        return holder.get("run_id") in _HELD
    return pid_alive(pid)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
