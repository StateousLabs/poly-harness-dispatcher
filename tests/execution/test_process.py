import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import FAKE_HARNESS, STREAMS
from helpers import wait_dead
from phd.execution import process as launcher
from phd.execution.process import HarnessProcess, child_env


def make(tmp_path, stdin="hello"):
    return HarnessProcess([sys.executable, str(FAKE_HARNESS)], env=child_env({}), cwd=tmp_path,
                          stdin_text=stdin, stderr_path=tmp_path / "stderr.log")


async def collect(proc):
    return [line async for line in proc.lines()]


async def test_streams_lines_and_exit_code(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    monkeypatch.setenv("FAKE_EXIT", "3")
    p = make(tmp_path)
    await p.start()
    lines = await collect(p)
    assert len(lines) == 5 and json.loads(lines[-1])["type"] == "result"
    assert await p.wait() == 3


async def test_line_longer_than_64k(tmp_path, monkeypatch):  # Review Focus 2
    big = tmp_path / "big.jsonl"
    big.write_text(json.dumps({"type": "user", "blob": "x" * 1_000_000}) + "\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(big))
    p = make(tmp_path)
    await p.start()
    lines = await collect(p)
    assert len(json.loads(lines[0])["blob"]) == 1_000_000
    assert await p.wait() == 0


async def test_stdin_is_utf8(tmp_path, monkeypatch):  # Review Focus 5
    echo = tmp_path / "echo.jsonl"
    monkeypatch.setenv("FAKE_ECHO_FILE", str(echo))
    p = make(tmp_path, stdin="# Message\n\nÜnïcode — café\n")
    await p.start()
    await collect(p)
    await p.wait()
    assert json.loads(echo.read_text(encoding="utf-8"))["stdin"] == "# Message\n\nÜnïcode — café\n"


async def test_kill_tree_kills_grandchildren(tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CHILD_PIDFILE", str(pidfile))
    monkeypatch.setenv("FAKE_SLEEP", "60")
    p = make(tmp_path)
    await p.start()
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.05)
    assert pidfile.exists() and pidfile.read_text(), "harness never wrote the grandchild pid"
    grandchild = int(pidfile.read_text())
    p.kill_tree()
    await asyncio.wait_for(p.wait(), timeout=15)
    assert p.killed
    assert await asyncio.to_thread(wait_dead, grandchild), "grandchild survived kill_tree"


@pytest.mark.skipif(os.name != "nt", reason="the taskkill fallback exists only on Windows")
async def test_taskkill_fallback_kills_tree_without_a_job(tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CHILD_PIDFILE", str(pidfile))
    monkeypatch.setenv("FAKE_SLEEP", "60")
    p = make(tmp_path)
    await p.start()
    guard, p.guard = p.guard, None  # as if orphan protection were unavailable: the fallback runs
    try:
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text():
                break
            await asyncio.sleep(0.05)
        grandchild = int(pidfile.read_text())
        harness = p.proc.pid
        p.kill_tree()
        await asyncio.wait_for(p.wait(), timeout=15)
        assert await asyncio.to_thread(wait_dead, harness) and await asyncio.to_thread(wait_dead, grandchild), "taskkill fallback left a process alive"
    finally:
        p.guard = guard
        p.close()


def test_stderr_tail_reads_only_the_end(tmp_path):
    p = make(tmp_path)
    p.stderr_path.write_bytes(b"a" * 5_000_000 + "é-end".encode())
    assert p.stderr_tail(10).endswith("é-end") and len(p.stderr_tail(10)) <= 10
    assert make(tmp_path / "none").stderr_tail() == ""


def test_child_env_strips_host_harness_vars(monkeypatch):  # Review Focus 1
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-leak")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/host/.claude")
    monkeypatch.setenv("OPENAI_API_KEY", "host-leak")
    monkeypatch.setenv("XDG_DATA_HOME", "/host/data")
    monkeypatch.setenv("PATH_LIKE_KEEP", "kept")
    env = child_env({"CLAUDE_CONFIG_DIR": "/run/home"})
    assert env["CLAUDE_CONFIG_DIR"] == "/run/home" and env["PATH_LIKE_KEEP"] == "kept"
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env and "XDG_DATA_HOME" not in env


async def test_overlong_line_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "STREAM_LIMIT", 1024)
    big = tmp_path / "big.jsonl"
    big.write_text("x" * 10_000 + "\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(big))
    monkeypatch.setenv("FAKE_SLEEP", "30")
    p = make(tmp_path)
    await p.start()
    lines = await collect(p)
    assert lines == [] and p.overflowed is True
    await asyncio.wait_for(p.wait(), timeout=15)


async def test_wait_returns_when_descendant_holds_stderr(tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CHILD_PIDFILE", str(pidfile))
    monkeypatch.setenv("FAKE_SLEEP", "0")
    p = make(tmp_path)
    await p.start()
    try:
        start = time.monotonic()
        await asyncio.wait_for(p.wait(), timeout=10)
        assert time.monotonic() - start < 10
    finally:
        if pidfile.exists() and pidfile.read_text():
            pid = int(pidfile.read_text())
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                os.kill(pid, 9)
