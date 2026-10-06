"""POSIX guard: a reaper process kills the harness's process group if this process goes away."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("reaper_main.py")


class ReaperGuard:
    def __init__(self) -> None:
        self.warning: str | None = None
        self._proc: subprocess.Popen | None = None

    def attach(self, pid: int) -> None:
        try:
            # Own session: a terminal Ctrl+C or a signal to the caller's group must not take the reaper too.
            self._proc = subprocess.Popen([sys.executable, "-I", str(SCRIPT), str(pid)], stdin=subprocess.PIPE,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          start_new_session=True, env={"PATH": os.environ.get("PATH", "")})
        except OSError as e:
            self.warning = f"orphan protection unavailable: reaper did not start ({e})"

    def kill(self) -> bool:
        return False  # PHD kills the group itself (killpg); the reaper only covers PHD's own death

    def release(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.write(b"exit\n")
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
