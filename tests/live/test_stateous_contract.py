"""Live proof of the Stateous requirements B5, B6, B7, B8 and B10 (`phd.md` section 3), once per harness.

B1-B4 and B9 are proven offline (see "Stateous contract" in docs/concepts/run-lifecycle.md). Run with:

    PHD_LIVE=1 uv run pytest tests/live/test_stateous_contract.py -v

Every test uses the routine's API shape: `async with Dispatcher(spec).run(inputs, session_id=...) as handle`,
`handle.events()`, `handle.session_id`, `handle.wait()`, `ev.summary`, `result.status`, `result.structured_output`,
`result.error.kind`."""

import asyncio
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from phd import Credentials, Dispatcher, Inputs, Spec, Stack
from phd.spec.output_schema import validate_answer
from test_live import ALLOW_ECHO, FIXTURES, HARNESSES, MODELS, RUNS, pytestmark  # noqa: F401  (pytestmark gates on PHD_LIVE)

CALLER = Path(__file__).with_name("live_crash_caller.py")

# What a Stateous state hands over: a root oneOf, a const `event`, an optional `payload`, no extra keys.
STATEOUS_SCHEMA = {"oneOf": [
    {"type": "object",
     "properties": {"event": {"const": "done"},
                    "payload": {"type": "object", "properties": {"summary": {"type": "string"}},
                                "required": ["summary"], "additionalProperties": False}},
     "required": ["event"], "additionalProperties": False},
    {"type": "object",
     "properties": {"event": {"const": "stuck"},
                    "payload": {"type": "object", "properties": {"reason": {"type": "string"}},
                                "required": ["reason"], "additionalProperties": False}},
     "required": ["event"], "additionalProperties": False}]}


def make_spec(harness: str, cwd: Path, **kw) -> Spec:
    return Spec(harness=harness, model=MODELS[harness], stack=Stack.load(FIXTURES / "stack"), cwd=cwd,
                credentials=Credentials.from_host(harness), secrets={"ECHO_PREFIX": "phd-echo"},
                harness_config=ALLOW_ECHO[harness], timeout_s=300, **kw)


async def drive(spec: Spec, inputs: Inputs, session_id: str | None = None):
    """The routine's loop. Returns (result, [(event, handle.session_id when it was delivered)], handle)."""
    seen = []
    async with Dispatcher(spec, runs_root=RUNS).run(inputs, session_id=session_id) as handle:
        async for ev in handle.events():
            seen.append((ev, handle.session_id))
        result = await handle.wait()
        return result, seen, handle


@pytest.mark.parametrize("harness", HARNESSES)
async def test_b5_session_id_at_start(harness, tmp_path):
    result, seen, handle = await drive(make_spec(harness, tmp_path), Inputs(msg="Reply with the word hi."))
    assert result.status == "succeeded", result.error
    types = [ev.type for ev, _ in seen]
    assert "session.started" in types, types
    first = types.index("session.started")
    assert first < types.index("run.finished"), types
    assert seen[first][1], "handle.session_id was not set when the first session-carrying event was delivered"
    assert seen[first][1] == handle.session_id == result.session_id


@pytest.mark.parametrize("harness", HARNESSES)
async def test_b6_unknown_session(harness, tmp_path):
    result, seen, handle = await drive(make_spec(harness, tmp_path), Inputs(msg="Reply with the word hi."),
                                       session_id=f"made-up-{uuid.uuid4()}")
    assert result.status != "succeeded"
    assert result.error is not None and result.error.kind == "session", result.error
    types = [ev.type for ev, _ in seen]
    assert "session.started" not in types, "a harness was started for an unknown session"
    assert not result.home_dir and result.exit_code is None, (result.home_dir, result.exit_code)


@pytest.mark.parametrize("harness", HARNESSES)
async def test_b6_resume_after_crash(harness, tmp_path):
    """A routine saves `handle.session_id`, its process is killed mid-turn, the retry resumes the session."""
    word = f"KUMQUAT-{uuid.uuid4().hex[:6].upper()}"
    prompt = (f"Remember this code word for later: {word}. Then count slowly from 1 to 300, one number per line, "
              "and do not stop early.")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(CALLER), harness, MODELS[harness], str(FIXTURES / "stack"), str(tmp_path), str(RUNS),
        prompt, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        line = (await asyncio.wait_for(proc.stdout.readline(), 120)).decode().strip()
        assert line.startswith("READY "), (line, (await proc.stderr.read(2000)).decode(errors="replace"))
        saved = line.split(" ", 1)[1]
        assert saved and saved != "None"
        await asyncio.sleep(2)  # mid-turn: the model is working on the counting
    finally:
        proc.kill()  # kill -9 / TerminateProcess: no cleanup runs in the caller
        await proc.wait()
    await asyncio.sleep(5)  # the harness dies with its caller (B3); give its lock a moment to go stale

    result, seen, handle = await drive(make_spec(harness, tmp_path),
                                       Inputs(msg="What was the code word I asked you to remember? Reply briefly."),
                                       session_id=saved)
    assert result.status == "succeeded", (
        f"{harness} could not resume a session killed mid-turn: {result.error}")
    assert word in (result.result_text or ""), (
        f"{harness} resumed the killed session but lost its memory of {word}: {result.result_text!r}")
    assert result.session_id == saved


@pytest.mark.parametrize("harness", HARNESSES)
async def test_b7_root_oneof(harness, tmp_path):
    result, _, _ = await drive(make_spec(harness, tmp_path, output_schema=STATEOUS_SCHEMA), Inputs(
        msg="The work is finished. Report it: event done, with payload.summary set to a short sentence."))
    assert result.status == "succeeded", result.error
    out = result.structured_output
    assert isinstance(out, dict) and "outcome" not in out, out  # unwrapped for the routine
    assert out["event"] == "done", out
    assert validate_answer(STATEOUS_SCHEMA, out) is None, out


@pytest.mark.parametrize("harness", HARNESSES)
async def test_b8_schema_mismatch(harness, tmp_path):
    """Forcing a model to violate a schema reliably is not possible live (the harnesses constrain or repair the
    answer), so the mismatch itself is proven offline: `tests/execution/test_structured.py`
    (`test_native_structured_output_that_does_not_match`, `test_non_json_answer`, ...). Live, this proves the other
    half on every harness: a run that succeeds carries a `structured_output` that validates against the *original*
    schema (not the harness's translated one)."""
    result, _, _ = await drive(make_spec(harness, tmp_path, output_schema=STATEOUS_SCHEMA), Inputs(
        msg="You cannot make progress. Report it: event stuck, with payload.reason set to a short sentence."))
    assert result.status == "succeeded", result.error
    assert result.error is None
    assert validate_answer(STATEOUS_SCHEMA, result.structured_output) is None, result.structured_output


@pytest.mark.parametrize("harness", HARNESSES)
async def test_b10_summaries(harness, tmp_path):
    result, seen, _ = await drive(make_spec(harness, tmp_path), Inputs(
        msg="Run the shell command `echo hello-b10` and tell me what it printed."))
    assert result.status == "succeeded", result.error
    by_type: dict[str, list[str]] = {}
    for ev, _sid in seen:
        by_type.setdefault(ev.type, []).append(ev.summary)
        if ev.type in ("message", "tool_call", "tool_result"):
            assert "\n" not in ev.summary and "\r" not in ev.summary, (ev.type, ev.summary)
            assert len(ev.summary) <= 500, (ev.type, len(ev.summary))
        else:
            assert ev.summary == "", (ev.type, ev.summary)
    assert any(by_type.get("tool_call", [])), f"no non-empty tool_call summary: {by_type}"
    assert any(by_type.get("tool_result", [])), f"no non-empty tool_result summary: {by_type}"
