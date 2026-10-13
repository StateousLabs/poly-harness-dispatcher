"""The streamed `usage` event carries the same per-turn cost as `RunResult` (claude reports cumulatively on --resume)."""

import json
import os
import sys

import pytest

from conftest import FAKE_HARNESS, STREAMS
from phd import Dispatcher, Inputs

FAKE = [sys.executable, str(FAKE_HARNESS)]
SCHEMA = {"type": "object", "properties": {"event": {"enum": ["done", "blocked"]}}, "required": ["event"]}


def claude_stream(tmp_path, name, *, cost, structured=None, session="sess-u1"):
    final = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "ok",
             "session_id": session, "total_cost_usd": cost, "usage": {"input_tokens": 10, "output_tokens": 5}}
    if structured is not None:
        final["structured_output"] = structured
    path = tmp_path / f"{name}.jsonl"
    lines = [{"type": "system", "subtype": "init", "session_id": session}, final]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return path


@pytest.fixture
def streams(tmp_path, monkeypatch):
    def use(*paths):
        monkeypatch.setenv("FAKE_STREAMS", os.pathsep.join(str(p) for p in paths))
        monkeypatch.setenv("FAKE_STATE", str(tmp_path / "state"))
    return use


def dispatcher(spec, tmp_path):
    return Dispatcher(spec, runs_root=tmp_path / "runs", validate_model=False, allow_temp_runs_root=True,
                      executables={spec.harness: FAKE})


async def run_and_collect(d, inputs, session_id=None):
    """(RunResult, the usage events the caller saw, the usage events in events.jsonl)."""
    handle = await d.start(inputs, session_id=session_id)
    seen = [ev.data for ev in [e async for e in handle.events()] if ev.type == "usage"]
    result = await handle.wait()
    with open(result.events_path, encoding="utf-8") as f:
        logged = [e["data"] for e in map(json.loads, f) if e["type"] == "usage"]
    return result, seen, logged


async def test_resumed_turn_event_carries_its_own_cost(tmp_path, make_spec, streams):
    streams(claude_stream(tmp_path, "t1", cost=0.30), claude_stream(tmp_path, "t2", cost=0.42))
    d = dispatcher(make_spec("claude"), tmp_path)
    first, _, _ = await run_and_collect(d, Inputs(msg="x"))
    second, seen, logged = await run_and_collect(d, Inputs(msg="again"), first.session_id)
    assert second.cost_usd == pytest.approx(0.12) and second.session_cost_usd == pytest.approx(0.42)
    assert len(seen) == 1
    assert seen[0]["cost_usd"] == pytest.approx(0.12) and seen[0]["session_cost_usd"] == pytest.approx(0.42)
    assert logged == seen  # events.jsonl holds what the caller saw


async def test_fresh_turn_event_is_unchanged(tmp_path, make_spec, streams):
    streams(claude_stream(tmp_path, "t1", cost=0.30))
    result, seen, logged = await run_and_collect(dispatcher(make_spec("claude"), tmp_path), Inputs(msg="x"))
    assert seen[0]["cost_usd"] == pytest.approx(0.30) and seen[0]["session_cost_usd"] == pytest.approx(0.30)
    assert seen[0]["cost_usd"] == result.cost_usd and seen[0]["session_cost_usd"] == result.session_cost_usd
    assert logged == seen


async def test_reported_below_the_baseline_is_the_turns_own_cost(tmp_path, make_spec, streams):
    streams(claude_stream(tmp_path, "t1", cost=0.30), claude_stream(tmp_path, "t2", cost=0.10))
    d = dispatcher(make_spec("claude"), tmp_path)
    first, _, _ = await run_and_collect(d, Inputs(msg="x"))
    second, seen, logged = await run_and_collect(d, Inputs(msg="again"), first.session_id)
    assert second.cost_usd == pytest.approx(0.10) and second.session_cost_usd == pytest.approx(0.10)
    assert seen[0]["cost_usd"] == pytest.approx(0.10) and seen[0]["session_cost_usd"] == pytest.approx(0.10)
    assert logged == seen


async def test_repair_turn_events_add_up_to_the_result(tmp_path, make_spec, streams):
    streams(claude_stream(tmp_path, "bad", cost=0.01, structured={"event": "nope"}),
            claude_stream(tmp_path, "good", cost=0.03, structured={"event": "done"}))  # cumulative: 0.02 own
    d = dispatcher(make_spec("claude", output_schema=SCHEMA, schema_repair=True), tmp_path)
    result, seen, logged = await run_and_collect(d, Inputs(msg="x"))
    assert "schema repair turn used" in result.warnings and result.cost_usd == pytest.approx(0.03)
    assert [e["cost_usd"] for e in seen] == [pytest.approx(0.01), pytest.approx(0.02)]
    assert sum(e["cost_usd"] for e in seen) == pytest.approx(result.cost_usd)
    assert seen[-1]["session_cost_usd"] == pytest.approx(result.session_cost_usd) == pytest.approx(0.03)
    assert logged == seen


async def test_repair_on_a_resumed_turn_adds_up(tmp_path, make_spec, streams):
    streams(claude_stream(tmp_path, "t1", cost=0.30, structured={"event": "done"}),
            claude_stream(tmp_path, "bad", cost=0.35, structured={"event": "nope"}),
            claude_stream(tmp_path, "good", cost=0.42, structured={"event": "done"}))
    d = dispatcher(make_spec("claude", output_schema=SCHEMA, schema_repair=True), tmp_path)
    first, _, _ = await run_and_collect(d, Inputs(msg="x"))
    second, seen, _ = await run_and_collect(d, Inputs(msg="again"), first.session_id)
    assert second.cost_usd == pytest.approx(0.12) and second.session_cost_usd == pytest.approx(0.42)
    assert [e["cost_usd"] for e in seen] == [pytest.approx(0.05), pytest.approx(0.07)]
    assert [e["session_cost_usd"] for e in seen] == [pytest.approx(0.35), pytest.approx(0.42)]


@pytest.mark.parametrize("harness,stream", [("codex", "codex_ok.jsonl"), ("opencode", "opencode_ok.jsonl")])
async def test_per_invocation_harness_events_keep_their_cost(tmp_path, make_spec, monkeypatch, harness, stream):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / stream))
    raw = []
    from phd.harnesses import get_adapter
    parser = get_adapter(harness).parser()
    for line in (STREAMS / stream).read_text(encoding="utf-8").splitlines():
        raw += [data for kind, data in parser.feed(json.loads(line)) if kind == "usage"]
    result, seen, logged = await run_and_collect(dispatcher(make_spec(harness), tmp_path), Inputs(msg="x"))
    assert seen and [e["cost_usd"] for e in seen] == [e["cost_usd"] for e in raw]
    assert all(e["session_cost_usd"] is None for e in seen) and result.session_cost_usd is None
    assert logged == seen
