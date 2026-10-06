"""Tie each harness process tree to its caller's process, so a dying caller cannot leave an agent running.

Windows: a Job Object with kill-on-close (the harness is created suspended, assigned, then resumed).
POSIX: a small reaper process that kills the harness's process group when the caller goes away."""

from __future__ import annotations

import os
from typing import Protocol

CREATE_SUSPENDED = 0x00000004


class Guard(Protocol):
    warning: str | None

    def attach(self, pid: int) -> None:
        """Protect the just-started harness `pid` (on Windows: also resume it — always)."""

    def kill(self) -> bool:
        """Kill the whole tree; `False` when this guard cannot (the caller falls back to its own kill)."""

    def release(self) -> None:
        """Stop protecting (every finish path, after PHD's own kill/exit handling). Idempotent."""


def new_guard() -> Guard:
    if os.name == "nt":
        from .windows_job import JobGuard
        return JobGuard()
    from .reaper import ReaperGuard
    return ReaperGuard()


__all__ = ["CREATE_SUSPENDED", "Guard", "new_guard"]
