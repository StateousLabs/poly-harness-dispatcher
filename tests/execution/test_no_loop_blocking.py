"""The async API never blocks the event loop (B4): a ticker on the same loop keeps beating."""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from conftest import FAKE_HARNESS, STREAMS
from phd import Dispatcher, Inputs, RunStatus, Stack
from phd.errors import ModelNotSupported
from phd.harnesses import catalog as cat_mod

SLOW = [sys.executable, str(Path(FAKE_HARNESS).parent / "slow_catalog.py")]
INTERVAL = 0.25


@pytest.fixture(autouse=True)
def fresh_catalogue_cache():
    cat_mod.__dict__.get("_PROCESS_CACHE", {}).clear()
    yield
    cat_mod.__dict__.get("_PROCESS_CACHE", {}).clear()


@pytest.fixture
def count(tmp_path, monkeypatch):
    path = tmp_path / "catalog-count.txt"
    monkeypatch.setenv("FAKE_CATALOG_COUNT", str(path))
    return lambda: len(path.read_text().splitlines()) if path.exists() else 0


def codex_dispatcher(spec, tmp_path, **kw):
    return Dispatcher(spec, runs_root=tmp_path / "runs", allow_temp_runs_root=True,
                      executables={"codex": SLOW}, **kw)


def big_stack(tmp_path, n=3000) -> Stack:
    root = tmp_path / "big-stack"
    (root / "skills" / "bulk").mkdir(parents=True)
    (root / "skills" / "bulk" / "SKILL.md").write_text("---\nname: bulk\ndescription: bulk\n---\nbulk\n", encoding="utf-8")
    for i in range(n):
        (root / "skills" / "bulk" / f"f{i}.txt").write_text("x" * 2048, encoding="utf-8")
    (root / "agents").mkdir()
    (root / "agents" / "helper.md").write_text("# Identity: Helper\n\nBe brief.\n", encoding="utf-8")
    (root / "stack.json").write_text(json.dumps({"name": "big", "version": "1", "harnesses": ["claude", "codex", "opencode"]}),
                                     encoding="utf-8")
    return Stack.load(root)


async def test_ticker_keeps_beating_through_a_run(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "codex_ok.jsonl"))
    monkeypatch.setenv("FAKE_LINE_DELAY", "0.3")
    monkeypatch.setenv("FAKE_CATALOG_DELAY", "1.5")
    spec = make_spec("codex", stack=big_stack(tmp_path))   # loading a stack is the caller's blocking work
    stamps: list[float] = []
    stop = asyncio.Event()

    async def ticker():
        while not stop.is_set():
            stamps.append(time.monotonic())
            await asyncio.sleep(INTERVAL)

    t = asyncio.create_task(ticker())
    await asyncio.sleep(INTERVAL * 2)
    d = codex_dispatcher(spec, tmp_path)          # built on the loop, as a caller would
    async with d.run(inputs) as handle:
        result = await handle.wait()
    stop.set()
    await t
    assert result.status is RunStatus.SUCCEEDED, result.error
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert max(gaps) < 2 * INTERVAL, f"event loop stalled: max gap {max(gaps):.2f}s"


def test_unknown_codex_model_not_raised_by_constructor(tmp_path, make_spec, count):
    d = codex_dispatcher(make_spec("codex", model="gpt-nope"), tmp_path)   # no subprocess, no raise
    assert count() == 0


async def test_unknown_codex_model_raised_from_start_and_run(tmp_path, make_spec, inputs, count):
    d = codex_dispatcher(make_spec("codex", model="gpt-nope"), tmp_path)
    with pytest.raises(ModelNotSupported):
        await d.start(inputs)
    with pytest.raises(ModelNotSupported):
        async with d.run(inputs):
            pass
    assert not (tmp_path / "runs").exists() or not list((tmp_path / "runs").glob("*/home"))  # nothing launched


def test_unknown_codex_model_raised_from_plan(tmp_path, make_spec, inputs):
    d = codex_dispatcher(make_spec("codex", model="gpt-nope"), tmp_path)
    with pytest.raises(ModelNotSupported):
        d.plan(inputs)


def test_unknown_claude_model_still_raised_by_constructor(tmp_path, make_spec):
    with pytest.raises(ModelNotSupported):
        Dispatcher(make_spec("claude", model="gpt-4o"), runs_root=tmp_path / "runs")


def test_catalogue_runs_once_per_process(tmp_path, make_spec, inputs, count):
    for i in range(3):
        d = codex_dispatcher(make_spec("codex"), tmp_path / f"d{i}")
        d.plan(inputs)
    assert count() == 1


async def test_catalogue_warning_lands_in_result(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "codex_ok.jsonl"))
    monkeypatch.setenv("FAKE_CATALOG_FAIL", "1")
    d = codex_dispatcher(make_spec("codex"), tmp_path)
    async with d.run(inputs) as handle:
        result = await handle.wait()
    assert result.status is RunStatus.SUCCEEDED, result.error
    assert any("model catalogue" in w for w in result.warnings)
