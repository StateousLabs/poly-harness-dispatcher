"""The hook shim is copied into each run's home and executed there, so coverage never attributes the subprocess runs in
test_hooks.py to `phd/materialize/hooks/shim.py`. These tests run the packaged module's `main` in-process against a
written runtime, covering the same branches."""

import io
import json
import sys
from pathlib import Path

import pytest

from phd._util.shell import shell_join
from phd.materialize import hooks
from phd.materialize.hooks import shim as hook_shim


def _py(code: str) -> str:
    return shell_join([Path(sys.executable).as_posix(), "-c", code])


@pytest.fixture
def run_shim(tmp_path, monkeypatch, capsys):
    def run(hook: dict | None, argv_tail=("claude", "0"), stdin: str = "{}", *, delete=()):
        runtime = hooks.write_runtime(tmp_path / "home", run_id="run-1", run_dir=tmp_path,
                                      hooks=[hook] if hook else [])
        monkeypatch.setattr(hook_shim, "HERE", runtime.parent)
        for name in delete:
            (runtime.parent / name).unlink()
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = hook_shim.main(["hook_shim.py", *argv_tail])
        out = capsys.readouterr()
        return code, out.out, out.err
    return run


def hook(event="pre_tool", command=None, timeout_s=10):
    return {"event": event, "tool": None, "command": command or _py("pass"), "timeout_s": timeout_s}


def test_pre_tool_allow_relays_stdout(run_shim):
    code, out, _ = run_shim(hook(command=_py("print('hi')")), stdin=json.dumps({"tool_name": "Bash"}))
    assert code == 0 and out.strip() == "hi"


def test_pre_tool_block_exit_2_keeps_the_guard_message_only(run_shim):
    code, _, err = run_shim(hook(command=_py("import sys; sys.stderr.write('no'); sys.exit(2)")))
    assert code == 2 and err == "no"


def test_pre_tool_other_nonzero_fails_closed_with_reason(run_shim):
    code, _, err = run_shim(hook(command=_py("import sys; sys.exit(3)")))
    assert code == 2 and "guard exited 3" in err


def test_post_tool_nonzero_maps_to_1(run_shim):
    code, _, _ = run_shim(hook("post_tool", _py("import sys; sys.exit(2)")))
    assert code == 1


def test_event_carries_run_id_and_neutral_tool(run_shim, tmp_path):
    seen = tmp_path / "seen.json"
    cmd = _py("import json,os,sys; open(os.path.join(os.environ['PHD_RUN_DIR'], 'seen.json'),'w').write(sys.stdin.read())")
    code, _, _ = run_shim(hook(command=cmd), stdin=json.dumps({"tool_name": "mcp__echo__echo", "tool_input": {"a": 1}}))
    event = json.loads(seen.read_text(encoding="utf-8"))
    assert code == 0 and event["tool"] == "mcp:echo/echo" and event["run_id"] == "run-1" and event["input"] == {"a": 1}


def test_opencode_payload_is_passed_through_neutral():
    raw = {"event": "pre_tool", "tool": "shell"}
    assert hook_shim.to_neutral("opencode", "pre_tool", raw) is raw


def test_invalid_arguments(run_shim):
    code, _, err = run_shim(hook(), argv_tail=("claude",))
    assert code == 2 and "invalid arguments" in err
    code, _, err = run_shim(hook(), argv_tail=("claude", "x"))
    assert code == 2 and "invalid arguments" in err


def test_hook_index_out_of_range(run_shim):
    code, _, err = run_shim(hook(), argv_tail=("claude", "5"))
    assert code == 2 and "hooks.resolved.json" in err


def test_missing_shim_config(run_shim):
    code, _, err = run_shim(hook(), delete=("shim_config.json",))
    assert code == 2 and "shim_config.json" in err


@pytest.mark.parametrize("event,expected", [("pre_tool", 2), ("post_tool", 1)])
def test_invalid_stdin_json(run_shim, event, expected):
    code, _, err = run_shim(hook(event), stdin="not json")
    assert code == expected and "invalid stdin JSON" in err


@pytest.mark.parametrize("event,expected", [("pre_tool", 2), ("post_tool", 1)])
def test_hook_timeout(run_shim, event, expected):
    code, _, err = run_shim(hook(event, _py("import time; time.sleep(5)"), timeout_s=0.2))
    assert code == expected and "timed out" in err


def test_unrunnable_hook_command(run_shim, monkeypatch):
    def boom(*a, **k):
        raise OSError("no shell")
    monkeypatch.setattr(hook_shim.subprocess, "run", boom)
    code, _, err = run_shim(hook())
    assert code == 2 and "failed to run hook command" in err


def test_non_object_stdin_is_an_error(run_shim):
    code, _, err = run_shim(hook(), stdin="[1]")
    assert code == 2 and "PHD hook shim error" in err
