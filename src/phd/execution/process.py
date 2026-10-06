"""Launcher (CoreDesign §2d): run one harness CLI with the prompt on stdin, stream its stdout
line by line, and kill the whole process tree on timeout or cancel."""

import asyncio
import os
import signal
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

from . import lifetime

HOST_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "CODEX_", "OPENAI_", "OPENCODE_", "XDG_")
STREAM_LIMIT = 16 * 1024 * 1024  # Claude stream-json lines carry whole tool results


def child_env(overrides: dict[str, str]) -> dict[str, str]:
    """The host environment minus every harness/auth variable, plus PHD's own values."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(HOST_ENV_PREFIXES)}
    env.update(overrides)
    return env


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
        self.guard = lifetime.new_guard()
        self.guard.attach(self.proc.pid)  # on Windows this also resumes the suspended harness
        self._stderr_task = asyncio.create_task(self._copy_stderr())
        # The prompt is written in the background: a harness that never reads stdin must not block
        # start() (and so escape timeout_s).
        self._stdin_task = asyncio.create_task(self._write_stdin())

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
            result = subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
            if result.returncode != 0 and self.proc.returncode is None:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
        else:
            self.killed = True
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def stderr_tail(self, limit: int = 2000) -> str:
        try:
            return self.stderr_path.read_text(encoding="utf-8", errors="replace")[-limit:]
        except FileNotFoundError:
            return ""

    async def _copy_stderr(self) -> None:
        with open(self.stderr_path, "ab") as f:
            while chunk := await self.proc.stderr.read(65536):
                f.write(chunk)
