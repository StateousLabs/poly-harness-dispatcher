"""Per-home lock: two turns must never use (and rewrite) the same harness session at once.

Every run that uses a home holds `<home>/.phd-session.lock` while it runs: a fresh run on its new
home, a resumed turn on the session's shared home (taken before the home is re-materialized). The
file records `{"pid", "run_id", "started_at"}`. A lock whose process is gone, or whose file has been unreadable
for a grace period, is stale and is taken over (under `<home>/.phd-session.lock.takeover`, an OS lock, so two acquirers
cannot both delete it and each end up running). Several runs can live in one Python process (async), so for a lock
written by this process the in-process registry `_HELD` decides whether its run is still going.

The credential sweep (`credential_copies`) briefly holds a home's lock too, as a *sweeper* (`"sweep": true` in the
file; one delete long). An acquirer that meets a live sweeper, or a lock file that is still being written, waits up to
`BRIEF_HOLD_WAIT_S` for it to go before it reports the home busy.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .._util.filelock import FileLock, is_stale, read_holder, retry_sharing
from .._util.proc import pid_alive
from ..errors import NotResumable

LOCK_NAME = ".phd-session.lock"
TAKEOVER_NAME = LOCK_NAME + ".takeover"  # an OS lock (FileLock) that serializes taking over a stale lock; never deleted
TAKEOVER_TIMEOUT_S = 5.0
BRIEF_HOLD_WAIT_S = 2.0  # how long an acquirer waits out a sweeper's hold or a lock file being written
POLL_S = 0.05
SWEEP_TAKEOVER_TIMEOUT_S = 0.5  # a sweeper skips a home rather than queue behind another takeover
_HELD: set[str] = set()  # run_ids that hold a lock in this process


class SessionLock:
    """The lock on one run home, held by one run. `acquire` raises `NotResumable` when busy.

    `SessionLock.sweeper(home)` is the credential sweep's lock: it is marked in the file, never waits for another
    holder, and gives up on a contended takeover quickly."""

    def __init__(self, home: Path, run_id: str, *, sweep: bool = False):
        self.path = Path(home) / LOCK_NAME
        self.run_id = run_id
        self.sweep = sweep
        self.held = False

    @classmethod
    def sweeper(cls, home: Path) -> SessionLock:
        return cls(home, f"phd-sweep-{uuid.uuid4().hex}", sweep=True)

    def acquire(self, what: str) -> SessionLock:
        """Take the lock or raise `NotResumable("<what> is busy …")`. `what` names the session.

        A live sweeper's hold, or a lock file between its create and its write, is waited out for up to
        `BRIEF_HOLD_WAIT_S` (a sweeper itself never waits)."""
        deadline = time.monotonic() + (0.0 if self.sweep else BRIEF_HOLD_WAIT_S)
        takeovers = 0
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                holder = read_holder(self.path)
                waiting = time.monotonic() < deadline
                if holder is not None and _running(holder):
                    if holder.get("sweep") is True and waiting:
                        time.sleep(POLL_S)
                        continue
                    raise NotResumable(_busy(what, holder)) from None
                if holder is None and not self.path.exists():  # released between our two looks: just retry
                    if waiting or takeovers < 3:
                        takeovers += 1
                        continue
                    raise NotResumable(f"{what}: could not acquire {self.path}") from None
                if holder is None and not is_stale(self.path):
                    if waiting:
                        time.sleep(POLL_S)
                        continue
                    raise NotResumable(f"{what} is busy (lock file is being written)") from None
                takeovers += 1
                if takeovers > 3:
                    raise NotResumable(f"{what}: could not acquire {self.path}") from None
                self._take_over(what, holder)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                record = {"pid": os.getpid(), "run_id": self.run_id,
                          "started_at": datetime.now(timezone.utc).isoformat()}
                json.dump(record | {"sweep": True} if self.sweep else record, f)
            self.held = True
            _HELD.add(self.run_id)
            return self

    def _take_over(self, what: str, holder: dict | None) -> None:
        """Delete the stale lock `holder` we looked at, unless another acquirer replaced it meanwhile.

        The re-check and the unlink run under the takeover lock: otherwise a second acquirer could take over and
        write its own lock between our check and our unlink, and we would delete that live lock."""
        if not self.path.parent.is_dir():  # the home is gone: do not let the lock file recreate it; acquire retries
            return
        try:
            timeout = SWEEP_TAKEOVER_TIMEOUT_S if self.sweep else TAKEOVER_TIMEOUT_S
            with FileLock(self.path.with_name(TAKEOVER_NAME), timeout_s=timeout):
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


def _busy(what: str, holder: dict) -> str:
    if holder.get("sweep") is True:  # never show the sweep's internal id as if it were a run
        return f"{what} is busy (PHD is deleting a leftover credential copy in it)"
    return f"{what} is busy with run {holder.get('run_id')} (pid {holder.get('pid')})"


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
