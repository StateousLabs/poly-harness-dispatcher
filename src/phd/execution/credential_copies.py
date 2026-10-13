"""Credential files an adapter copies into a run home (codex `auth.json`) exist only while a turn runs.

The run lifecycle deletes them when the turn ends, however it ends (`drop`, before the session lock is released, so
a turn that takes the lock next never loses the copy it just wrote). Copies left by a PHD process that was killed are
swept once per process per runs root (`sweep_once`): a run home whose session lock the sweep can take (as a marked
*sweeper*, see `SessionLock.sweeper`) has no live run, so its copies go; a home a live run holds keeps them. Taking
the lock (rather than only looking at it) means no turn can lock the home and write a fresh copy between the check
and the delete; an acquirer that meets the sweeper waits for it briefly instead of reporting busy.

The sweep never follows a link: a run directory, `home` or copy that is a symlink or junction, or a home that
resolves outside the runs root, is skipped, so nothing outside the runs root is deleted or written.
"""

from __future__ import annotations

import os
import threading
import warnings
from collections.abc import Callable
from pathlib import Path

from .._util.filelock import retry_sharing
from .._util.fs import is_link
from ..errors import NotResumable
from .session_lock import SessionLock

_SWEPT: set[str] = set()  # runs roots swept by this process
_ROOT_LOCKS: dict[str, threading.Lock] = {}  # one per runs root: a plan on that root waits for its sweep
_ROOT_LOCKS_GUARD = threading.Lock()


def _unlink(path: Path) -> None:
    try:
        retry_sharing(lambda: os.unlink(path))
    except FileNotFoundError:
        pass


def drop(paths: list[Path]) -> list[str]:
    """Delete `paths` (a link is removed as a link); never raises. Returns one message per file that could not be
    deleted."""
    problems = []
    for path in paths:
        try:
            _unlink(path)
        except OSError as e:
            problems.append(f"could not delete the credential copy {path} (the next PHD process using this runs "
                            f"root deletes it): {e}")
    return problems


def warn(problems: list[str]) -> None:
    """Report cleanup problems with `warnings.warn`, but never raise from cleanup: under warnings-as-errors the
    warning would replace the exception being handled (or skip the lock release), so it is dropped instead."""
    for problem in problems:
        try:
            warnings.warn(problem, RuntimeWarning, stacklevel=3)
        except Exception:
            pass


def sweep_once(root: Path, secret_paths: Callable[[Path], list[Path]]) -> None:
    """Delete the credential copies (`secret_paths(home)`) in every `<root>/<run>/home` that no live run holds.

    Runs once per process for each `root`; later calls return at once (a call while the sweep runs waits for it).
    One listing of `root`, then one look per run. Blocking (file locks, deletes): call it off the event loop. Never
    raises; a copy that cannot be deleted is warned about and left for the next process."""
    resolved = Path(root).resolve()
    key = os.path.normcase(str(resolved))
    with _ROOT_LOCKS_GUARD:
        root_lock = _ROOT_LOCKS.setdefault(key, threading.Lock())
    with root_lock:
        if key in _SWEPT:
            return
        _SWEPT.add(key)
        try:
            runs = list(os.scandir(resolved))
        except OSError:  # no runs root yet, or unreadable: nothing to sweep
            return
        for entry in runs:
            try:
                home = _run_home(resolved, entry)
                present = ([p for p in secret_paths(home) if os.path.lexists(p) and not is_link(p)]
                           if home is not None else [])
                if present:
                    _sweep_home(home, present)
            except Exception as e:  # one odd entry must not stop the sweep or fail the caller's run
                warn([f"credential sweep skipped {entry.path}: {e}"])


def _run_home(root: Path, entry: os.DirEntry) -> Path | None:
    """`<entry>/home` if `entry` is a real run directory of `root` with a real home in it, else None."""
    run_dir = Path(entry.path)
    if not entry.is_dir(follow_symlinks=False) or is_link(run_dir) or not (run_dir / "run.json").is_file():
        return None
    home = run_dir / "home"
    if is_link(home) or not home.is_dir() or home.resolve() != root / entry.name / "home":
        return None
    return home


def _sweep_home(home: Path, present: list[Path]) -> None:
    lock = SessionLock.sweeper(home)
    try:
        lock.acquire(f"run home {home}")
    except (NotResumable, OSError):  # a live run holds it (or the home just went away): keep its copy
        return
    try:
        warn(drop(present))
    finally:
        try:
            lock.release()
        except OSError as e:
            warn([f"could not release the sweep's lock {lock.path}: {e}"])
