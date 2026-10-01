"""Launcher (CoreDesign §2d): run one harness CLI with the prompt on stdin, stream its stdout
line by line, and kill the whole process tree on timeout or cancel."""

import asyncio
import os
import signal
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

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
        self.killed = False
        self._stderr_task: asyncio.Task | None = None

    async def start(self) -> None:
        if os.name == "nt":
            group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            group = {"start_new_session": True}
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, env=self.env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=STREAM_LIMIT, **group)
        self._stderr_task = asyncio.create_task(self._copy_stderr())
        try:
            self.proc.stdin.write(self.stdin_text.encode("utf-8"))
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.proc.stdin.close()

    async def lines(self) -> AsyncIterator[str]:
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                return
            yield raw.decode("utf-8", errors="replace").rstrip("\r\n")

    async def wait(self) -> int:
        code = await self.proc.wait()
        if self._stderr_task is not None:
            await self._stderr_task
        return code

    def kill_tree(self) -> None:
        if self.proc is None or self.proc.returncode is not None:
            return
        self.killed = True
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
        else:
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
