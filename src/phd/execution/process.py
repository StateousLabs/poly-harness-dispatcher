"""Launcher (CoreDesign §2d): run one harness CLI with the prompt on stdin, stream its stdout
line by line, and kill the whole process tree on timeout or cancel."""

import asyncio
import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import AsyncIterator
from pathlib import Path

from .._util.env import HOST_ENV_PREFIXES
from .._util.env import clean_env as _clean_env
from . import lifetime

__all__ = ["HOST_ENV_PREFIXES", "HarnessProcess", "child_env"]
STREAM_LIMIT = 16 * 1024 * 1024  # Claude stream-json lines carry whole tool results


def child_env(overrides: dict[str, str]) -> dict[str, str]:
    """The host environment minus every harness/auth variable, plus PHD's own values."""
    return _clean_env(overrides)


class HarnessProcess:
    def __init__(self, argv: list[str], *, env: dict[str, str], cwd: Path, stdin_text: str, stderr_path: Path):
        self.argv, self.env, self.cwd = argv, env, cwd
        self.stdin_text, self.stderr_path = stdin_text, stderr_path
        self.proc: asyncio.subprocess.Process | None = None
        self.guard: lifetime.Guard | None = None
        self.killed = False
        self.overflowed = False
        self._stderr_task: asyncio.Task | None = None
        self._stdin_task: asyncio.Task | None = None

    async def start(self) -> None:
        if os.name == "nt":
            group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | lifetime.CREATE_SUSPENDED}
        else:
            group = {"start_new_session": True}
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, env=self.env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=STREAM_LIMIT, **group)
        try:
            self.guard = lifetime.new_guard()
            self.guard.attach(self.proc.pid)  # on Windows this also resumes the suspended harness
        except BaseException:
            # Never leak a harness. It has spawned nothing yet (on Windows it is still suspended),
            # so killing the one process (POSIX: its new group) is enough.
            self._kill_unguarded()
            self.close()  # frees a job handle the guard may already hold
            try:
                await asyncio.wait_for(self.proc.wait(), 5)  # reap it: never leave the transport unawaited
            except Exception:
                pass
            raise
        self._stderr_task = asyncio.create_task(self._copy_stderr())
        # The prompt is written in the background: a harness that never reads stdin must not block
        # start() (and so escape timeout_s).
        self._stdin_task = asyncio.create_task(self._write_stdin())

    def _kill_unguarded(self) -> None:
        try:
            if os.name == "nt":
                self.proc.kill()
            else:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    self.proc.kill()
        except ProcessLookupError:
            pass

    async def _write_stdin(self) -> None:
        try:
            self.proc.stdin.write(self.stdin_text.encode("utf-8"))
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                self.proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    async def lines(self) -> AsyncIterator[str]:
        while True:
            try:
                raw = await self.proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                self.overflowed = True
                self.kill_tree()
                return
            if not raw:
                return
            yield raw.decode("utf-8", errors="replace").rstrip("\r\n")

    async def wait(self) -> int:
        # Process.wait() only resolves once every inherited pipe has closed, so a descendant
        # holding stdout/stderr would hang it; poll the transport's returncode instead.
        while self.proc.returncode is None:
            await asyncio.sleep(0.05)
        self.close()  # at once: the job ends orphaned descendants without waiting for the stderr grace
        code = self.proc.returncode
        if self._stdin_task is not None and not self._stdin_task.done():
            self._stdin_task.cancel()  # the process is gone; a pending write can never finish
        if self._stdin_task is not None:
            await asyncio.gather(self._stdin_task, return_exceptions=True)
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), 5)
            except asyncio.TimeoutError:
                self._stderr_task.cancel()  # a descendant still holds stderr open
        self.close()
        return code

    def close(self) -> None:
        """Release the lifetime guard (idempotent)."""
        if self.guard is not None:
            self.guard.release()

    @staticmethod
    def _taskkill_fallback(proc) -> None:
        """Start `taskkill /T /F` without waiting for it (this can run on the event loop); a daemon thread reaps it
        and falls back to `proc.kill()` if taskkill failed. `wait()` observes the process exit as usual."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        def kill_directly() -> None:
            if proc.returncode is None:
                try:
                    proc.kill()
                except OSError:
                    pass

        try:
            tk = subprocess.Popen(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            kill_directly()
            return

        def reap() -> None:
            if tk.wait() != 0:
                if loop is None:
                    kill_directly()
                else:  # the asyncio process object belongs to the loop's thread
                    with contextlib.suppress(RuntimeError):  # the loop has closed
                        loop.call_soon_threadsafe(kill_directly)

        threading.Thread(target=reap, daemon=True, name="phd-taskkill").start()

    def kill_tree(self) -> None:
        if self._stdin_task is not None and not self._stdin_task.done():
            self._stdin_task.cancel()
        if self.proc is None:
            return
        if self.guard is not None and self.guard.kill():  # Windows job: one call, reaches orphaned descendants too
            self.killed = True
            return
        if os.name == "nt":
            # Fallback when there is no job (protection unavailable).
            if self.proc.returncode is not None:
                return
            self.killed = True
            self._taskkill_fallback(self.proc)
        else:
            self.killed = True
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def stderr_tail(self, limit: int = 2000) -> str:
        try:
            with open(self.stderr_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 4 * limit))  # a few KB is plenty for `limit` characters
                return f.read().decode("utf-8", errors="replace")[-limit:]
        except FileNotFoundError:
            return ""

    async def _copy_stderr(self) -> None:
        with open(self.stderr_path, "ab") as f:
            while chunk := await self.proc.stderr.read(65536):
                f.write(chunk)
