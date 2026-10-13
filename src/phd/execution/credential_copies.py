"""Credential files an adapter copies into a run home (codex `auth.json`) exist only while a turn runs.

The run lifecycle deletes them when the turn ends, however it ends (`drop`, before the session lock is released, so
a turn that takes the lock next never loses the copy it just wrote). Copies left by a PHD process that was killed are
swept once per process per runs root (`sweep_once`): a run home whose session lock this sweep can take has no live
run, so its copies go; a home a live run holds keeps them. Taking the lock (rather than only looking at it) means no
turn can lock the home and write a fresh copy between the check and the delete.
"""

from __future__ import annotations

import os
import threading
import uuid
import warnings
from collections.abc import Callable
from pathlib import Path

from .._util.filelock import retry_sharing
from ..errors import NotResumable
from .session_lock import SessionLock

_SWEPT: set[str] = set()  # runs roots swept by this process
_SWEEP_LOCK = threading.Lock()


def _unlink(path: Path) -> None:
    try:
        retry_sharing(lambda: os.unlink(path))
    except FileNotFoundError:
        pass


def drop(paths: list[Path]) -> list[str]:
    """Delete `paths`; never raises. Returns one message per file that could not be deleted."""
    problems = []
    for path in paths:
        try:
            _unlink(path)
        except OSError as e:
            problems.append(f"could not delete the credential copy {path} (the next PHD process using this runs "
                            f"root deletes it): {e}")
    return problems


def sweep_once(root: Path, secret_paths: Callable[[Path], list[Path]]) -> None:
    """Delete the credential copies (`secret_paths(home)`) in every `<root>/<run>/home` that no live run holds.

    Runs once per process for each `root`; later calls return at once. One listing of `root`, then one look per run.
    Blocking (file locks, deletes): call it off the event loop. Never raises; a copy that cannot be deleted is
    reported with `warnings.warn` and left for the next process."""
    key = os.path.normcase(str(Path(root).resolve()))
    with _SWEEP_LOCK:
        if key in _SWEPT:
            return
        _SWEPT.add(key)
        try:
            runs = list(os.scandir(root))
        except OSError:  # no runs root yet, or unreadable: nothing to sweep
            return
        for entry in runs:
            home = Path(entry.path) / "home"
            present = [p for p in secret_paths(home) if os.path.lexists(p)]
            if present:
                _sweep_home(home, present)


def _sweep_home(home: Path, present: list[Path]) -> None:
    lock = SessionLock(home, f"phd-sweep-{uuid.uuid4().hex}")
    try:
        lock.acquire(f"run home {home}")
    except (NotResumable, OSError):  # a live run holds it (or the home just went away): keep its copy
        return
    try:
        for problem in drop(present):
            warnings.warn(problem, RuntimeWarning, stacklevel=3)
    finally:
        try:
            lock.release()
        except OSError as e:
            warnings.warn(f"could not release the sweep's lock {lock.path}: {e}", RuntimeWarning, stacklevel=3)
