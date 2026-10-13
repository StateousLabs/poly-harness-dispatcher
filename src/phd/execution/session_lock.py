"""Per-home lock: two turns must never use (and rewrite) the same harness session at once.

Every run that uses a home holds `<home>/.phd-session.lock` while it runs: a fresh run on its new
home, a resumed turn on the session's shared home (taken before the home is re-materialized). The
file records `{"pid", "run_id", "started_at"}`. A lock whose process is gone, or whose file has been unreadable
for a grace period, is stale and is taken over (under `<home>/.phd-session.lock.takeover`, an OS lock, so two acquirers
cannot both delete it and each end up running). Several runs can live in one Python process (async), so for a lock
written by this process the in-process registry `_HELD` decides whether its run is still going.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .._util.filelock import FileLock, is_stale, read_holder, retry_sharing
from .._util.proc import pid_alive
from ..errors import NotResumable

LOCK_NAME = ".phd-session.lock"
TAKEOVER_NAME = LOCK_NAME + ".takeover"  # an OS lock (FileLock) that serializes taking over a stale lock; never deleted
TAKEOVER_TIMEOUT_S = 5.0
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
                holder = read_holder(self.path)
                if holder is not None and _running(holder):
                    raise NotResumable(f"{what} is busy with run {holder.get('run_id')} (pid {holder.get('pid')})") from None
                if holder is None and not self.path.exists():  # released between our two looks: just retry
                    continue
                if holder is None and not is_stale(self.path):
                    raise NotResumable(f"{what} is busy (lock file is being written)") from None
                self._take_over(what, holder)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "run_id": self.run_id,
                           "started_at": datetime.now(timezone.utc).isoformat()}, f)
            self.held = True
            _HELD.add(self.run_id)
            return self
        raise NotResumable(f"{what}: could not acquire {self.path}")

    def _take_over(self, what: str, holder: dict | None) -> None:
        """Delete the stale lock `holder` we looked at, unless another acquirer replaced it meanwhile.

        The re-check and the unlink run under the takeover lock: otherwise a second acquirer could take over and
        write its own lock between our check and our unlink, and we would delete that live lock."""
        if not self.path.parent.is_dir():  # the home is gone: do not let the lock file recreate it; acquire retries
            return
        try:
            with FileLock(self.path.with_name(TAKEOVER_NAME), timeout_s=TAKEOVER_TIMEOUT_S):
                current = read_holder(self.path)
                # an unreadable file may be a new lock between its create and its write: stale only after the grace
                if current == holder and (holder is not None or is_stale(self.path)):
                    _unlink(self.path)
        except TimeoutError:
            raise NotResumable(f"{what} is busy (another run is taking over its stale lock)") from None

    def release(self) -> None:
        """Drop the lock if this run still holds it. Safe to call more than once."""
        if not self.held:
            return
        self.held = False
        try:
            holder = read_holder(self.path)
            if holder is not None and holder.get("run_id") == self.run_id:
                _unlink(self.path)
        finally:
            # only after the unlink: until then a concurrent acquirer must still see this run as holding the lock,
            # or it would take the "stale" lock over and we would delete its fresh one
            _HELD.discard(self.run_id)


def _running(holder: dict) -> bool:
    pid = holder.get("pid")
    if not isinstance(pid, int):
        return False
    if pid == os.getpid():
        return holder.get("run_id") in _HELD
    return pid_alive(pid)


def _unlink(path: Path) -> None:
    try:
        retry_sharing(lambda: os.unlink(path))
    except FileNotFoundError:
        pass
