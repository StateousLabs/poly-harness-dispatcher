import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from conftest import FAKE_HARNESS, STREAMS
from helpers import wait_dead
from phd import Dispatcher, Inputs, RunStatus, Stack
from phd.errors import InvalidRunsRoot, NotResumable, UnsupportedFeature

FAKE = [sys.executable, str(FAKE_HARNESS)]


def dispatcher(spec, tmp_path, **kw):
    kw.setdefault("runs_root", tmp_path / "runs")
    kw.setdefault("validate_model", False)
    kw.setdefault("allow_temp_runs_root", True)
    return Dispatcher(spec, executables={spec.harness: FAKE}, **kw)


@pytest.fixture
def echo(tmp_path, monkeypatch):
    path = tmp_path / "echo.jsonl"
    monkeypatch.setenv("FAKE_ECHO_FILE", str(path))
    return lambda: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_claude_spawn_success(tmp_path, monkeypatch, make_spec, inputs, echo, workdir):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-leak")          # Review Focus 1
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/host/.claude")
    d = dispatcher(make_spec("claude"), tmp_path)
    r = d.spawn(inputs)

    assert r.status is RunStatus.SUCCEEDED and r.error is None
    assert r.session_id == "sess-claude-1" and r.cost_usd == pytest.approx(0.0284) and r.cli_version == "2.1.286"
    assert r.result_text.startswith("HELPER-IDENTITY-OK") and r.resumable
    assert r.stack == "fixture-helper@0.1.0" and r.stack_hash.startswith("sha256:")
    assert r.cwd == str(workdir) and r.credential_kind == "claude_oauth_token"

    call = echo()[0]
    assert call["argv"][0] == "-p" and "--append-system-prompt-file" in call["argv"]
    assert call["stdin"].startswith("# Task brief") and "café" in call["stdin"]
    assert call["cwd"] == str(workdir)
    assert call["env"]["CLAUDE_CONFIG_DIR"] == r.home_dir and call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "test-oauth-token"
    assert "ANTHROPIC_API_KEY" not in call["env"]

    assert d.ledger.get(r.run_id).to_dict() == r.to_dict()
    record = json.loads((tmp_path / "runs" / r.run_id / "run.json").read_text(encoding="utf-8"))
    assert record["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "<redacted>"
    assert "test-oauth-token" not in (tmp_path / "runs" / r.run_id / "result.json").read_text(encoding="utf-8")
    types = [json.loads(line)["type"] for line in open(r.events_path, encoding="utf-8")]
    assert types[0] == "run.started" and types[-1] == "run.finished" and "tool_call" in types


async def test_events_stream(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "opencode_ok.jsonl"))
    handle = await dispatcher(make_spec("opencode"), tmp_path).start(inputs)
    types = [ev.type async for ev in handle.events()]
    assert types == ["run.started", "session.started", "tool_call", "usage", "message", "usage", "run.finished"]
    assert (await handle.wait()).status is RunStatus.SUCCEEDED


def test_non_json_stdout_becomes_log_event(tmp_path, monkeypatch, make_spec, inputs):  # Review Focus 3
    stream = tmp_path / "noisy.jsonl"
    stream.write_text("Reading additional input from stdin...\n" + (STREAMS / "codex_ok.jsonl").read_text(encoding="utf-8"),
                      encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(stream))
    r = dispatcher(make_spec("codex"), tmp_path).spawn(inputs)
    assert r.status is RunStatus.SUCCEEDED and r.cost_source == "unknown"
    logs = [json.loads(l) for l in open(r.events_path, encoding="utf-8") if json.loads(l)["type"] == "log"]
    assert logs[0]["data"]["text"] == "Reading additional input from stdin..."


def test_resume_by_session_id(tmp_path, monkeypatch, make_spec, inputs, echo):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(inputs)
    second = d.spawn(Inputs(msg="now fix the test"), session_id=first.session_id)
    third = asyncio.run(_start_and_wait(d, Inputs(msg="and again"), session_id="sess-claude-1"))
    calls = echo()
    assert calls[1]["argv"][-2:] == ["--resume", "sess-claude-1"]
    assert calls[2]["argv"].count("--resume") == 1
    assert calls[1]["stdin"] == "# Message\n\nnow fix the test\n"
    assert calls[1]["env"]["CLAUDE_CONFIG_DIR"] == first.home_dir
    assert second.home_dir == first.home_dir == third.home_dir
    assert second.resumed_from == first.run_id and third.resumed_from == second.run_id  # latest run of the session
    assert len({first.run_id, second.run_id, third.run_id}) == 3 and second.run_dir != first.run_dir
    record = json.loads((tmp_path / "runs" / second.run_id / "run.json").read_text(encoding="utf-8"))
    assert record["session_id"] == "sess-claude-1" and record["resumed_from"] == first.run_id
    assert "base_argv" not in record


async def _start_and_wait(d, inputs, **kw):
    return await (await d.start(inputs, **kw)).wait()


def test_resume_with_brief_only(tmp_path, monkeypatch, make_spec, inputs, echo):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(inputs)
    r = d.spawn(Inputs(brief=inputs.brief), session_id=first.session_id)
    assert r.status is RunStatus.SUCCEEDED and echo()[1]["stdin"].startswith("# Task brief")


def test_resume_rejects_rules_before_writing(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(inputs)
    before = sorted((tmp_path / "runs").iterdir())
    home_files = sorted((str(p), p.stat().st_mtime_ns) for p in Path(first.home_dir).rglob("*"))
    with pytest.raises(UnsupportedFeature, match="rules cannot be passed when resuming"):
        d.spawn(inputs, session_id=first.session_id)
    assert sorted((tmp_path / "runs").iterdir()) == before
    assert sorted((str(p), p.stat().st_mtime_ns) for p in Path(first.home_dir).rglob("*")) == home_files


def test_resume_unknown_session(tmp_path, make_spec):
    r = dispatcher(make_spec("claude"), tmp_path).spawn(Inputs(msg="hi"), session_id="no-such-session")
    assert r.status is RunStatus.FAILED and r.error.kind == "session" and "no-such-session" in r.error.message
    assert [p.name for p in (tmp_path / "runs").iterdir()] == [r.run_id]  # only the failed run itself
    with pytest.raises(NotResumable, match="no-such-session"):
        dispatcher(make_spec("claude"), tmp_path).plan(Inputs(msg="hi"), session_id="no-such-session")


def test_resume_other_harness_or_cwd(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    first = dispatcher(make_spec("claude"), tmp_path).spawn(inputs)
    r = dispatcher(make_spec("opencode"), tmp_path).spawn(Inputs(msg="hi"), session_id=first.session_id)
    assert r.error.kind == "session" and "'claude'" in r.error.message
    other = tmp_path / "elsewhere"
    other.mkdir()
    r = dispatcher(make_spec("claude", cwd=other), tmp_path).spawn(Inputs(msg="hi"), session_id=first.session_id)
    assert r.error.kind == "session" and "cwd" in r.error.message


SYSTEM_FILE = {"claude": "system-prompt.md", "codex": "AGENTS.md", "opencode": "AGENTS.md"}
OK_STREAM = {"claude": "claude_ok.jsonl", "codex": "codex_ok.jsonl", "opencode": "opencode_ok.jsonl"}


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode"])
def test_resume_reapplies_the_session_rules(harness, tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / OK_STREAM[harness]))
    d = dispatcher(make_spec(harness), tmp_path)
    first = d.spawn(inputs)
    system_file = Path(first.home_dir) / SYSTEM_FILE[harness]
    system_file.write_text("stale", encoding="utf-8")
    d.spawn(Inputs(msg="next"), session_id=first.session_id)
    system = system_file.read_text(encoding="utf-8")
    assert "HELPER-IDENTITY-OK" in system and "## Project rules (binding)" in system and "RULES-OK" in system


def test_rules_text_is_carried_along_the_chain(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(inputs)
    second = d.spawn(Inputs(msg="two"), session_id=first.session_id)
    third = d.spawn(Inputs(msg="three"), session_id=first.session_id)
    expected = inputs.rules.read_text(encoding="utf-8").strip()
    for r in (first, second, third):
        assert d.ledger.read_record(r.run_id)["rules_text"] == expected
    assert third.resumed_from == second.run_id


def test_resume_of_a_session_without_rules_adds_none(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(Inputs(msg="first"))
    second = d.spawn(Inputs(msg="next"), session_id=first.session_id)
    assert "## Project rules" not in (Path(first.home_dir) / "system-prompt.md").read_text(encoding="utf-8")
    assert d.ledger.read_record(first.run_id)["rules_text"] is None
    assert d.ledger.read_record(second.run_id)["rules_text"] is None


def test_resume_needs_the_run_record(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(inputs)
    with pytest.raises(NotResumable, match="not a run home"):  # the home's own run dir must hold a run.json
        (Path(first.run_dir) / "run.json").rename(Path(first.run_dir) / "run.json.bak")
        d.spawn(Inputs(msg="next"), session_id=first.session_id)
    (Path(first.run_dir) / "run.json.bak").rename(Path(first.run_dir) / "run.json")
    second = d.spawn(Inputs(msg="next"), session_id=first.session_id)
    (Path(second.run_dir) / "run.json").unlink()  # the latest turn carries the session's rules_text
    with pytest.raises(NotResumable, match="has no run.json"):
        d.spawn(Inputs(msg="again"), session_id=first.session_id)


def test_codex_resume_command(tmp_path, monkeypatch, make_spec, inputs, echo, workdir):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "codex_ok.jsonl"))
    d = dispatcher(make_spec("codex"), tmp_path)
    d.spawn(Inputs(msg="continue"), session_id=d.spawn(inputs).session_id)
    resumed = echo()[1]
    assert resumed["argv"][:3] == ["exec", "resume", "01a0f55a-d4f3-7d43-943f-8708f4dfa0cf"]
    assert "-C" not in resumed["argv"] and resumed["cwd"] == str(workdir)


def test_not_resumable_without_home(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude", resumable=False), tmp_path)
    r = d.spawn(inputs)
    assert not r.resumable
    assert not Path(r.home_dir).exists() and (Path(r.run_dir) / "result.json").is_file()
    again = d.spawn(Inputs(msg="more"), session_id=r.session_id)
    assert again.error.kind == "session" and "no longer exists" in again.error.message


def test_resume_applies_the_new_spec(tmp_path, monkeypatch, make_spec, inputs, echo, stack):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    first = dispatcher(make_spec("claude"), tmp_path).spawn(inputs)
    home = Path(first.home_dir)
    (home / "projects" / "p").mkdir(parents=True)
    (home / "projects" / "p" / "session.jsonl").write_text("history", encoding="utf-8")
    assert (home / "skills" / "demo-minimal").is_dir()

    slim = tmp_path / "slim-stack"
    shutil.copytree(stack.root, slim)
    shutil.rmtree(slim / "skills" / "demo-minimal")
    meta = json.loads((slim / "stack.json").read_text(encoding="utf-8")) | {"version": "0.2.0"}
    (slim / "stack.json").write_text(json.dumps(meta), encoding="utf-8")
    spec = make_spec("claude", stack=Stack.load(slim), autonomy="full", unsafe=True,
                     harness_config={"env": {"PHD_TEST": "1"}})
    r = dispatcher(spec, tmp_path).spawn(Inputs(msg="next"), session_id=first.session_id)

    argv = echo()[1]["argv"]
    assert "--dangerously-skip-permissions" in argv and "--permission-mode" not in argv
    assert json.loads((home / "settings.json").read_text(encoding="utf-8"))["env"] == {"PHD_TEST": "1"}
    assert (home / "projects" / "p" / "session.jsonl").read_text(encoding="utf-8") == "history"
    assert not (home / "skills" / "demo-minimal").exists() and (home / "skills" / "demo-notes").is_dir()
    assert r.stack == "fixture-helper@0.2.0" and r.stack_hash == Stack.load(slim).content_hash()
    assert r.stack_hash != first.stack_hash
    assert any("fixture-helper@0.1.0" in w and "fixture-helper@0.2.0" in w for w in r.warnings)


def test_resume_hooks_log_to_the_new_run(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(inputs)
    r = d.spawn(Inputs(msg="next"), session_id=first.session_id)
    shim_config = json.loads((Path(first.home_dir) / "phd" / "shim_config.json").read_text(encoding="utf-8"))
    assert shim_config == {"run_id": r.run_id, "run_dir": r.run_dir}


def test_resumed_run_never_deletes_the_shared_home(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    first = dispatcher(make_spec("claude"), tmp_path).spawn(inputs)
    r = dispatcher(make_spec("claude", resumable=False), tmp_path).spawn(Inputs(msg="next"),
                                                                          session_id=first.session_id)
    assert r.status is RunStatus.SUCCEEDED and Path(first.home_dir).is_dir() and r.resumable


def _snapshot(root):
    return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in Path(root).rglob("*"))


def test_plan_a_resumed_turn_never_touches_the_session(tmp_path, monkeypatch, make_spec, inputs, echo):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(inputs)
    before = _snapshot(first.home_dir)
    m = d.plan(Inputs(msg="next"), session_id=first.session_id)
    assert _snapshot(first.home_dir) == before
    assert m.home == m.run_dir / "home" and str(m.home) != first.home_dir
    assert m.argv[-2:] == ["--resume", "sess-claude-1"] and m.stdin == "# Message\n\nnext\n" and len(echo()) == 1
    assert "RULES-OK" in (m.home / "system-prompt.md").read_text(encoding="utf-8")  # session rules applied
    record = json.loads((m.run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["resumed_from"] == first.run_id and record["session_id"] == "sess-claude-1"


def _run_dirs(tmp_path):
    return sorted((tmp_path / "runs").iterdir())


def test_tampered_home_dir_is_refused(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(inputs)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "settings.json").write_text("mine", encoding="utf-8")
    (decoy / "auth.json").write_text("secret", encoding="utf-8")
    (decoy / "skills").mkdir()
    result_path = Path(first.run_dir) / "result.json"
    data = json.loads(result_path.read_text(encoding="utf-8")) | {"home_dir": str(decoy)}
    result_path.write_text(json.dumps(data), encoding="utf-8")
    before, decoy_before = _run_dirs(tmp_path), _snapshot(decoy)
    with pytest.raises(NotResumable, match="not a run home"):
        d.spawn(Inputs(msg="next"), session_id=first.session_id)
    assert _snapshot(decoy) == decoy_before and _run_dirs(tmp_path) == before
    assert (decoy / "settings.json").read_text(encoding="utf-8") == "mine"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_junction_in_the_home_is_refused(tmp_path, monkeypatch, make_spec, inputs):
    import _winapi
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(inputs)
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    skills = Path(first.home_dir) / "skills"
    shutil.rmtree(skills)
    _winapi.CreateJunction(str(target), str(skills))
    with pytest.raises(NotResumable, match="symlink or junction"):
        d.spawn(Inputs(msg="next"), session_id=first.session_id)
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_failed_run_with_a_session_is_resumable(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    monkeypatch.setenv("FAKE_EXIT", "3")
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(inputs)
    assert first.status is RunStatus.FAILED and first.session_id == "sess-claude-1"
    monkeypatch.delenv("FAKE_EXIT")
    r = d.spawn(Inputs(msg="next"), session_id=first.session_id)
    assert r.status is RunStatus.SUCCEEDED and r.resumed_from == first.run_id


def test_refused_resumes_write_nothing(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    first = dispatcher(make_spec("claude"), tmp_path).spawn(inputs)
    other = tmp_path / "elsewhere"
    other.mkdir()
    before, home_before = _run_dirs(tmp_path), _snapshot(first.home_dir)
    for spec in (make_spec("opencode"), make_spec("claude", cwd=other)):
        r = dispatcher(spec, tmp_path).spawn(Inputs(msg="hi"), session_id=first.session_id)
        assert r.error.kind == "session"
    # only the two failed runs' own directories were added; the session's home is untouched
    assert len(_run_dirs(tmp_path)) == len(before) + 2 and _snapshot(first.home_dir) == home_before
    # a fresh resumable=False run reporting the same session id becomes its latest run, and deletes its home
    gone = dispatcher(make_spec("claude", resumable=False), tmp_path).spawn(Inputs(msg="x"))
    r = dispatcher(make_spec("claude"), tmp_path).spawn(Inputs(msg="hi"), session_id=gone.session_id)
    assert r.error.kind == "session" and "no longer exists" in r.error.message


def test_opencode_resume_argv(tmp_path, monkeypatch, make_spec, inputs, echo):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "opencode_ok.jsonl"))
    d = dispatcher(make_spec("opencode"), tmp_path)
    first = d.spawn(inputs)
    d.spawn(Inputs(msg="next"), session_id=first.session_id)
    assert first.session_id and echo()[1]["argv"][-2:] == ["--session", first.session_id]


def test_codex_auth_is_refreshed_on_resume(tmp_path, monkeypatch, make_spec, inputs, credentials):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "codex_ok.jsonl"))
    d = dispatcher(make_spec("codex"), tmp_path)
    first = d.spawn(inputs)
    credentials["codex"].path.write_text('{"tokens": {"access_token": "rotated"}}', encoding="utf-8")
    d.spawn(Inputs(msg="next"), session_id=first.session_id)
    assert "rotated" in (Path(first.home_dir) / "auth.json").read_text(encoding="utf-8")


def test_read_only_owned_file_is_replaced(tmp_path, monkeypatch, make_spec, inputs):
    import os
    import stat
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    d = dispatcher(make_spec("claude"), tmp_path)
    first = d.spawn(inputs)
    settings = Path(first.home_dir) / "settings.json"
    os.chmod(settings, stat.S_IREAD)
    spec = make_spec("claude", harness_config={"env": {"PHD_TEST": "2"}})
    dispatcher(spec, tmp_path).spawn(Inputs(msg="next"), session_id=first.session_id)
    assert json.loads(settings.read_text(encoding="utf-8"))["env"] == {"PHD_TEST": "2"}


def test_auth_failure_status(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_not_logged_in.jsonl"))
    monkeypatch.setenv("FAKE_EXIT", "1")
    r = dispatcher(make_spec("claude"), tmp_path).spawn(inputs)
    assert r.status is RunStatus.AUTH_FAILED and r.error.kind == "auth" and r.exit_code == 1


def test_crash_without_output(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_EXIT", "3")
    r = dispatcher(make_spec("claude"), tmp_path).spawn(inputs)
    assert r.status is RunStatus.FAILED and r.error.kind == "harness_crash" and "exit code 3" in r.error.message


def test_timeout_kills_tree(tmp_path, monkeypatch, make_spec, inputs):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CHILD_PIDFILE", str(pidfile))
    monkeypatch.setenv("FAKE_SLEEP", "60")
    t0 = time.monotonic()
    r = dispatcher(make_spec("claude", timeout_s=2), tmp_path).spawn(inputs)
    assert r.status is RunStatus.TIMED_OUT and time.monotonic() - t0 < 20
    assert wait_dead(int(pidfile.read_text()))


async def test_cancel(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_SLEEP", "60")
    handle = await dispatcher(make_spec("claude"), tmp_path).start(inputs)
    await asyncio.sleep(0.5)
    r = await handle.cancel()
    assert r.status is RunStatus.CANCELLED


def test_budget_enforced_from_stream(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "opencode_costly.jsonl"))
    monkeypatch.setenv("FAKE_SLEEP", "60")
    r = dispatcher(make_spec("opencode", budget_usd=0.1), tmp_path).spawn(inputs)
    assert r.status is RunStatus.BUDGET_EXCEEDED and r.cost_usd == pytest.approx(0.5)


def test_codex_refuses_temp_runs_root(tmp_path, make_spec):
    with pytest.raises(InvalidRunsRoot):
        Dispatcher(make_spec("codex"), runs_root=tmp_path / "runs", validate_model=False)


async def test_sync_spawn_inside_event_loop_raises(tmp_path, make_spec, inputs):
    with pytest.raises(RuntimeError, match="await dispatcher.start"):
        dispatcher(make_spec("claude"), tmp_path).spawn(inputs)


def test_plan_writes_home_without_running(tmp_path, make_spec, inputs, echo):
    m = dispatcher(make_spec("claude"), tmp_path).plan(inputs)
    assert (m.home / "settings.json").is_file() and (m.run_dir / "run.json").is_file()
    assert not (tmp_path / "echo.jsonl").exists()


def test_structured_output_from_result_text(tmp_path, monkeypatch, make_spec, inputs):
    stream = tmp_path / "codex_json.jsonl"
    stream.write_text('{"type":"thread.started","thread_id":"t1"}\n'
                      '{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"{\\"ok\\": true}"}}\n'
                      '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n', encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(stream))
    r = dispatcher(make_spec("codex", output_schema={"type": "object"}), tmp_path).spawn(inputs)
    assert r.structured_output == {"ok": True}


def test_stream_overflow_fails_run(tmp_path, monkeypatch, make_spec, inputs):
    import phd.execution.process
    monkeypatch.setattr(phd.execution.process, "STREAM_LIMIT", 1024)
    stream = tmp_path / "big.jsonl"
    stream.write_text(json.dumps({"type": "x", "pad": "a" * 10240}) + "\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", str(stream))
    r = dispatcher(make_spec("claude"), tmp_path).spawn(inputs)
    assert r.status is RunStatus.FAILED and r.error.kind == "stream_overflow"


async def test_events_second_iteration_terminates(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "opencode_ok.jsonl"))
    handle = await dispatcher(make_spec("opencode"), tmp_path).start(inputs)
    await handle.wait()
    first = [ev.type async for ev in handle.events()]
    second = await asyncio.wait_for(_drain(handle), 5)
    assert first[-1] == "run.finished" and second == []


async def _drain(handle):
    return [ev async for ev in handle.events()]


def test_home_cleanup_failure_is_a_warning(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))

    def boom(path):
        raise OSError("file in use")
    monkeypatch.setattr("phd.execution.handle.robust_rmtree", boom)
    d = dispatcher(make_spec("claude", resumable=False), tmp_path)
    r = d.spawn(inputs)
    assert r.status is RunStatus.SUCCEEDED and not r.resumable
    assert any("could not remove run home" in w and r.home_dir in w for w in r.warnings)
    assert (tmp_path / "runs" / r.run_id / "result.json").is_file()


async def test_stack_hash_pinned_at_plan_time(tmp_path, monkeypatch, make_spec, inputs):
    from phd.spec import Stack
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    monkeypatch.setenv("FAKE_SLEEP", "1")
    d = dispatcher(make_spec("claude"), tmp_path)
    handle = await d.start(inputs)

    def boom(self):
        raise OSError("stack moved")
    monkeypatch.setattr(Stack, "content_hash", boom)
    r = await handle.wait()
    assert r.status is RunStatus.SUCCEEDED and r.stack_hash.startswith("sha256:")


async def test_build_result_failure_still_yields_result(tmp_path, monkeypatch, make_spec, inputs):
    from phd.execution import RunHandle

    def boom(self, *a, **k):
        raise ValueError("bad outcome")
    monkeypatch.setattr(RunHandle, "_build_result", boom)
    handle = await dispatcher(make_spec("claude"), tmp_path).start(inputs)
    r = await asyncio.wait_for(handle.wait(), 20)
    assert r.status is not RunStatus.SUCCEEDED and r.error.kind == "phd_internal"
    assert "run.finished" in [ev.type async for ev in handle.events()]


async def test_interrupted_run_kills_process_tree(tmp_path, monkeypatch, make_spec, inputs):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CHILD_PIDFILE", str(pidfile))
    monkeypatch.setenv("FAKE_SLEEP", "60")
    handle = await dispatcher(make_spec("claude"), tmp_path).start(inputs)
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.1)
    grandchild, child = int(pidfile.read_text()), handle._proc.proc.pid
    handle._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle._task
    assert wait_dead(child) and wait_dead(grandchild)


def test_timeout_covers_a_stalled_stdin_write(tmp_path, monkeypatch, make_spec, inputs):
    from phd import Inputs
    monkeypatch.setenv("FAKE_NO_STDIN", "1")
    monkeypatch.setenv("FAKE_SLEEP", "30")
    big = Inputs(brief=inputs.brief, rules=inputs.rules, msg="x" * (2 * 1024 * 1024))
    t0 = time.monotonic()
    r = dispatcher(make_spec("claude", timeout_s=2), tmp_path).spawn(big)
    assert r.status is RunStatus.TIMED_OUT and time.monotonic() - t0 < 15


def test_plan_repr_hides_secrets(tmp_path, make_spec, inputs):
    assert "test-oauth-token" not in repr(dispatcher(make_spec("claude"), tmp_path).plan(inputs))


def test_exit_zero_without_final_result_fails(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_EXIT", "0")
    r = dispatcher(make_spec("claude"), tmp_path).spawn(inputs)
    assert r.status is RunStatus.FAILED and r.error.kind == "no_result"


def test_crash_stderr_tail_is_scrubbed(tmp_path, monkeypatch, make_spec, inputs):
    monkeypatch.setenv("FAKE_EXIT", "3")
    monkeypatch.setenv("FAKE_STDERR", "boom token=test-oauth-token prefix=phd-echo end")
    r = dispatcher(make_spec("claude"), tmp_path).spawn(inputs)
    assert r.error.kind == "harness_crash" and "boom" in r.error.message and "<redacted>" in r.error.message
    text = (tmp_path / "runs" / r.run_id / "result.json").read_text(encoding="utf-8")
    assert "test-oauth-token" not in text and "phd-echo" not in text
