"""POSIX guard: a reaper process kills the harness's process group if this process goes away."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

SCRIPT = Path(__file__).with_name("reaper_main.py")


class ReaperGuard:
    def __init__(self) -> None:
        self.warning: str | None = None
        self._proc: subprocess.Popen | None = None

    def attach(self, pid: int) -> None:
        if getattr(sys, "frozen", False):  # sys.executable is the frozen app, not a Python that can run the script
            self.warning = "orphan protection unavailable: frozen interpreter"
            return
        try:
            # Own session: a terminal Ctrl+C or a signal to the caller's group must not take the reaper too.
            self._proc = subprocess.Popen([sys.executable, "-I", str(SCRIPT), str(pid)], stdin=subprocess.PIPE,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          start_new_session=True, env={"PATH": os.environ.get("PATH", "")})
        except Exception as e:
            self.warning = f"orphan protection unavailable: reaper did not start ({type(e).__name__}: {e})"

    def kill(self) -> bool:
        return False  # PHD kills the group itself (killpg); the reaper only covers PHD's own death

    def release(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.write(b"exit\n")
        except OSError:
            pass  # the reaper already left (its group was gone)
        try:
            proc.stdin.close()
        except OSError:
            pass
        # Reap off the event loop: release() runs on every run end and must not block.
        threading.Thread(target=_reap, args=(proc,), name="phd-reaper-reap", daemon=True).start()


def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
