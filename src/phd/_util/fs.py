"""Filesystem helpers shared by the materializer, ledger and dispatcher."""

import json
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_json_atomic(path: Path, data: Any) -> None:
    """`write_json` through a temp file and a rename, so a reader never sees a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    write_json(tmp, data)
    os.replace(tmp, path)


def write_private(path: Path, data: bytes) -> None:
    """Write a file only the current user should read (credentials), byte for byte (no newline translation).

    On POSIX systems, creates the file with 0o600 permissions from the start to avoid
    a window where credentials are readable. On Windows, POSIX modes are ignored."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    finally:
        try:
            path.chmod(0o600)
        except OSError:
            pass


def deep_merge(base: dict, extra: dict, *, conflicts: list[str] | None = None, _path: str = "") -> dict:
    """Merge user-supplied `extra` into PHD-owned `base` without mutating either.

    Dicts merge recursively, lists are unioned (base order first), and when both sides hold
    different scalars `base` wins and the dotted key path is appended to `conflicts`."""
    out = dict(base)
    for key, value in extra.items():
        dotted = f"{_path}.{key}" if _path else str(key)
        if key not in out:
            out[key] = value
        elif isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value, conflicts=conflicts, _path=dotted)
        elif isinstance(out[key], list) and isinstance(value, list):
            out[key] = out[key] + [v for v in value if v not in out[key]]
        elif out[key] != value and conflicts is not None:
            conflicts.append(dotted)
    return out


def robust_rmtree(path: Path, attempts: int = 5) -> None:
    """rmtree that survives read-only files and Windows' transient sharing violations."""
    def make_writable_and_retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    for attempt in range(attempts):
        try:
            shutil.rmtree(path, onexc=make_writable_and_retry)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def remove_path(path: Path) -> None:
    """Delete a file, symlink, junction or directory tree; a missing path is fine.

    A symlink or junction is removed as a link: its target is never touched. A read-only file is made
    writable and deleted. Raises `OSError` if the path still exists afterwards."""
    if path.is_symlink() or path.is_junction():
        try:
            path.unlink()
        except (IsADirectoryError, PermissionError):
            os.rmdir(path)  # a Windows junction or directory symlink is removed like an empty directory
    elif path.is_dir():
        robust_rmtree(path)
    else:
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            path.unlink(missing_ok=True)
    if os.path.lexists(path):
        raise OSError(f"could not remove {path}")


def is_within(path: Path, parent: Path) -> bool:
    return Path(path).resolve().is_relative_to(Path(parent).resolve())
