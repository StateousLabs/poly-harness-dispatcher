import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from phd.materialize import hooks
from phd.materialize.hooks import translate
from phd._util.shell import shell_join
from phd.errors import HookUnsupported
from phd.spec import HookDef

GUARD = textwrap.dedent('''
    import json, os, sys
    evt = json.load(sys.stdin)
    with open(os.path.join(os.environ["PHD_RUN_DIR"], "seen.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(evt) + "\\n")
    if "rm -rf" in json.dumps(evt.get("input")):
        print("blocked: rm -rf", file=sys.stderr)
        sys.exit(2)
''')


def test_native_tool_names():
    assert hooks.native_tool("claude", "shell") == "Bash"
    assert hooks.native_tool("codex", "shell") == "Bash"
    assert hooks.native_tool("opencode", "web_fetch") == "webfetch"
    assert hooks.native_tool("claude", "mcp:echo/echo") == "mcp__echo__echo"
    with pytest.raises(HookUnsupported, match="codex"):
        hooks.native_tool("codex", "read")


def test_check_supported():
    hooks.check_supported("claude", [HookDef("stop", "x"), HookDef("pre_tool", "x", "read")])
    with pytest.raises(HookUnsupported, match="session_start"):
        hooks.check_supported("opencode", [HookDef("session_start", "x")])


def test_claude_style_block(tmp_path, monkeypatch):
    monkeypatch.setattr(translate, "_is_windows", lambda: False)
    shim = tmp_path / "home" / "phd" / "hook_shim.py"
    block = hooks.claude_style_block("codex", [{"event": "pre_tool", "tool": "shell", "command": "c", "timeout_s": 10.0}], shim)
    entry = block["PreToolUse"][0]
    assert entry["matcher"] == "Bash"
    assert entry["hooks"][0]["type"] == "command"
    # native timeout = ceil(timeout_s) + margin, so the shim's own timeout (fail-closed) fires first
    assert entry["hooks"][0]["timeout"] == 10 + translate.HOOK_TIMEOUT_MARGIN_S == 20
    assert isinstance(entry["hooks"][0]["timeout"], int)
    frac = hooks.claude_style_block("claude", [{"event": "pre_tool", "tool": "shell", "command": "c", "timeout_s": 2.5}], shim)
    assert frac["PreToolUse"][0]["hooks"][0]["timeout"] == 3 + translate.HOOK_TIMEOUT_MARGIN_S
    assert entry["hooks"][0]["command"] == translate._shim_command(shim, "codex", 0)


def test_opencode_plugin_is_filled_in(tmp_path):
    js = hooks.opencode_plugin([{"event": "pre_tool", "tool": "shell", "command": "c", "timeout_s": 5}], tmp_path / "s.py")
    assert "__HOOKS__" not in js and "__SHIM__" not in js
    assert '"event": "pre_tool"' in js and '"bash": "shell"' in js
    assert json.dumps((tmp_path / "s.py").as_posix()) in js


def test_shim_translates_payload_and_relays_block(tmp_path):
    guard = tmp_path / "guard.py"
    guard.write_text(GUARD, encoding="utf-8")
    command = shell_join([Path(sys.executable).as_posix(), guard.as_posix()])
    shim = hooks.write_runtime(tmp_path / "home", run_id="run-1", run_dir=tmp_path,
                               hooks=[{"event": "pre_tool", "tool": "shell", "command": command, "timeout_s": 10}])

    def call(harness, payload):
        return subprocess.run([sys.executable, str(shim), harness, "0"], input=json.dumps(payload),
                              text=True, capture_output=True)

    ok = call("claude", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                         "tool_input": {"command": "echo hi"}, "cwd": "/w"})
    assert ok.returncode == 0, ok.stderr
    blocked = call("codex", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                             "tool_input": {"command": "rm -rf build"}, "cwd": "/w"})
    assert blocked.returncode == 2 and "blocked: rm -rf" in blocked.stderr
    neutral = call("neutral", {"event": "pre_tool", "harness": "opencode", "tool": "shell",
                               "input": {"command": "ls"}, "cwd": "/w"})
    assert neutral.returncode == 0

    seen = [json.loads(line) for line in (tmp_path / "seen.jsonl").read_text(encoding="utf-8").splitlines()]
    assert seen[0] == {"event": "pre_tool", "harness": "claude", "tool": "shell", "input": {"command": "echo hi"},
                       "output": None, "cwd": "/w", "run_id": "run-1"}
    assert seen[1]["harness"] == "codex" and seen[1]["tool"] == "shell"
    assert seen[2] == {"event": "pre_tool", "harness": "opencode", "tool": "shell", "input": {"command": "ls"},
                       "cwd": "/w", "run_id": "run-1"}


def test_shim_maps_mcp_tool_names(tmp_path):
    guard = tmp_path / "guard.py"
    guard.write_text(GUARD, encoding="utf-8")
    command = shell_join([Path(sys.executable).as_posix(), guard.as_posix()])
    shim = hooks.write_runtime(tmp_path / "home", run_id="r", run_dir=tmp_path,
                               hooks=[{"event": "post_tool", "tool": None, "command": command, "timeout_s": 10}])
    subprocess.run([sys.executable, str(shim), "claude", "0"], text=True, capture_output=True,
                   input=json.dumps({"tool_name": "mcp__echo__echo", "tool_input": {}, "tool_response": "x"}))
    seen = json.loads((tmp_path / "seen.jsonl").read_text(encoding="utf-8"))
    assert seen["tool"] == "mcp:echo/echo" and seen["event"] == "post_tool" and seen["output"] == "x"


def test_neutral_tool_mcp_without_subpath():
    assert hooks.native_tool("claude", "mcp:echo") == "mcp__echo__.*"


def test_shim_error_on_invalid_stdin_json_pre_tool(tmp_path):
    guard = tmp_path / "guard.py"
    guard.write_text(GUARD, encoding="utf-8")
    command = shell_join([Path(sys.executable).as_posix(), guard.as_posix()])
    shim = hooks.write_runtime(tmp_path / "home", run_id="r", run_dir=tmp_path,
                               hooks=[{"event": "pre_tool", "tool": None, "command": command, "timeout_s": 10}])
    result = subprocess.run([sys.executable, str(shim), "claude", "0"], text=True,
                           capture_output=True, input="invalid json")
    assert result.returncode == 2
    assert "PHD hook shim error" in result.stderr


def test_shim_error_on_pre_tool_timeout(tmp_path):
    sleep_cmd = f"{Path(sys.executable).as_posix()} -c \"import time; time.sleep(5)\""
    shim = hooks.write_runtime(tmp_path / "home", run_id="r", run_dir=tmp_path,
                               hooks=[{"event": "pre_tool", "tool": None, "command": sleep_cmd, "timeout_s": 0.1}])
    result = subprocess.run([sys.executable, str(shim), "claude", "0"], text=True,
                           capture_output=True, input=json.dumps({"tool_name": "Bash"}))
    assert result.returncode == 2
    assert "PHD hook shim error" in result.stderr


def test_shim_error_code_for_post_tool_exit(tmp_path):
    exit_3_cmd = f"{Path(sys.executable).as_posix()} -c \"import sys; sys.exit(3)\""
    shim = hooks.write_runtime(tmp_path / "home", run_id="r", run_dir=tmp_path,
                               hooks=[{"event": "post_tool", "tool": None, "command": exit_3_cmd, "timeout_s": 10}])
    result = subprocess.run([sys.executable, str(shim), "claude", "0"], text=True,
                           capture_output=True, input=json.dumps({"tool_name": "Bash"}))
    assert result.returncode == 1


def test_claude_shell_matcher_covers_powershell(tmp_path):
    shim = tmp_path / "home" / "phd" / "hook_shim.py"
    hook = [{"event": "pre_tool", "tool": "shell", "command": "c", "timeout_s": 10}]
    assert hooks.claude_style_block("claude", hook, shim)["PreToolUse"][0]["matcher"] == "Bash|PowerShell"
    assert hooks.claude_style_block("codex", hook, shim)["PreToolUse"][0]["matcher"] == "Bash"


def test_shim_translates_powershell_to_shell(tmp_path):
    guard = tmp_path / "guard.py"
    guard.write_text("import json,sys\nopen(sys.argv[1],'w').write(json.dumps(json.load(sys.stdin)))\n",
                     encoding="utf-8")
    seen = tmp_path / "seen.json"
    command = shell_join([Path(sys.executable).as_posix(), guard.as_posix(), seen.as_posix()])
    shim = hooks.write_runtime(tmp_path / "home", run_id="run-1", run_dir=tmp_path,
                               hooks=[{"event": "pre_tool", "tool": "shell", "command": command, "timeout_s": 10}])
    r = subprocess.run([sys.executable, str(shim), "claude", "0"], text=True, capture_output=True,
                       input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "PowerShell",
                                         "tool_input": {"command": "echo hi"}, "cwd": "/w"}))
    assert r.returncode == 0, r.stderr
    assert json.loads(seen.read_text(encoding="utf-8"))["tool"] == "shell"


def test_claude_edit_matcher_covers_multiedit(tmp_path):
    shim = tmp_path / "home" / "phd" / "hook_shim.py"
    block = hooks.claude_style_block("claude", [{"event": "pre_tool", "tool": "edit", "command": "c", "timeout_s": 10}], shim)
    entry = block["PreToolUse"][0]
    assert entry["matcher"] == "Edit|MultiEdit"


def test_shim_error_on_non_object_json_pre_tool(tmp_path):
    guard = tmp_path / "guard.py"
    guard.write_text(GUARD, encoding="utf-8")
    command = shell_join([Path(sys.executable).as_posix(), guard.as_posix()])
    shim = hooks.write_runtime(tmp_path / "home", run_id="r", run_dir=tmp_path,
                               hooks=[{"event": "pre_tool", "tool": None, "command": command, "timeout_s": 10}])
    result = subprocess.run([sys.executable, str(shim), "claude", "0"], text=True,
                           capture_output=True, input=json.dumps([]))
    assert result.returncode == 2
    assert "PHD hook shim error" in result.stderr


def test_hook_shim_neutral_tool():
    from phd.materialize.hooks import shim as hook_shim
    assert hook_shim.neutral_tool("mcp__echo") == "mcp:echo"
    assert hook_shim.neutral_tool("mcp__echo__echo") == "mcp:echo/echo"


def test_codex_command_outer_quotes_only_for_spaced_interpreter_on_windows(tmp_path, monkeypatch):
    shim = tmp_path / "run dir" / "hook_shim.py"
    hook = [{"event": "pre_tool", "tool": "shell", "command": "c", "timeout_s": 10}]

    def command(harness):
        return hooks.claude_style_block(harness, hook, shim)["PreToolUse"][0]["hooks"][0]["command"]

    monkeypatch.setattr(sys, "executable", "C:/py/python.exe")
    monkeypatch.setattr(translate, "_is_windows", lambda: True)
    plain = translate._shim_command(shim, "codex", 0)
    assert not plain.startswith('"') and command("codex") == plain  # unchanged: the form verified live
    monkeypatch.setattr(sys, "executable", "C:/Program Files/py/python.exe")
    spaced = translate._shim_command(shim, "codex", 0)
    assert spaced.startswith('"') and command("codex") == f'"{spaced}"'
    assert command("claude") == translate._shim_command(shim, "claude", 0)
    monkeypatch.setattr(translate, "_is_windows", lambda: False)
    assert command("codex") == spaced


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe quoting is Windows-only")
def test_codex_hook_runs_under_cmd_exe_with_spaced_path(tmp_path, monkeypatch):
    run_dir = tmp_path / "run dir"
    run_dir.mkdir()
    guard = run_dir / "my guard.py"
    guard.write_text(GUARD, encoding="utf-8")
    real_python = sys.executable
    launcher = run_dir / "py launcher.bat"  # a spaced interpreter path too: four quotes, the case cmd.exe mangles
    launcher.write_text('@"' + real_python + '" %*' + chr(13) + chr(10), encoding="utf-8", newline="")
    monkeypatch.setattr(sys, "executable", str(launcher))
    hook_cmd = shell_join([Path(real_python).as_posix(), guard.as_posix()])
    shim = hooks.write_runtime(run_dir / "home", run_id="r", run_dir=run_dir,
                               hooks=[{"event": "pre_tool", "tool": "shell", "command": hook_cmd, "timeout_s": 10}])
    monkeypatch.setattr(translate, "_is_windows", lambda: True)
    command = hooks.claude_style_block("codex", [{"event": "pre_tool", "tool": "shell", "command": hook_cmd,
                                                  "timeout_s": 10}], shim)["PreToolUse"][0]["hooks"][0]["command"]

    def run(cmd):  # a string, not a list: list2cmdline would backslash-escape the quotes cmd.exe sees
        return subprocess.run(f"cmd.exe /C {cmd}", input=json.dumps(
            {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "echo hi"}, "cwd": "/w"}),
            text=True, capture_output=True)

    ok = run(command)
    assert ok.returncode == 0, ok.stderr
    assert (run_dir / "seen.jsonl").is_file()  # the shim really ran the guard
    blocked = subprocess.run(f"cmd.exe /C {command}", text=True, capture_output=True, input=json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}))
    assert blocked.returncode == 2 and "blocked: rm -rf" in blocked.stderr


def test_materialized_hooks_quote_spaced_stack_dir(tmp_path, make_spec, inputs):
    from phd import Dispatcher
    d = Dispatcher(make_spec("claude"), runs_root=tmp_path / "run dir", validate_model=False)
    m = d.plan(inputs)
    resolved = json.loads((m.home / "phd" / "hooks.resolved.json").read_text(encoding="utf-8"))
    stack = (m.home / "stack").as_posix()
    assert " " in stack
    assert resolved[0]["command"] == f'python "{stack}"/hooks/guard.py'  # adjacent quoted + bare text is one shell word'
    mcp = json.loads((m.home / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert any(stack + "/mcp/echo_server.py" in arg for srv in mcp.values() for arg in srv["args"])


def test_shim_non_pre_tool_exit_2_maps_to_1(tmp_path):
    cmd = f"{Path(sys.executable).as_posix()} -c \"import sys; sys.exit(2)\""
    shim = hooks.write_runtime(tmp_path / "home", run_id="r", run_dir=tmp_path,
                               hooks=[{"event": "stop", "tool": None, "command": cmd, "timeout_s": 10}])
    result = subprocess.run([sys.executable, str(shim), "claude", "0"], text=True, capture_output=True,
                            input=json.dumps({"hook_event_name": "Stop"}))
    assert result.returncode == 1


def _run_shim(tmp_path, event, command):
    shim = hooks.write_runtime(tmp_path / "home", run_id="r", run_dir=tmp_path,
                               hooks=[{"event": event, "tool": None, "command": command, "timeout_s": 10}])
    return subprocess.run([sys.executable, str(shim), "claude", "0"], text=True, capture_output=True,
                          input=json.dumps({"tool_name": "Bash"}))


def test_shim_pre_tool_missing_command_fails_closed(tmp_path):
    result = _run_shim(tmp_path, "pre_tool", "phd-no-such-command-xyz")
    assert result.returncode == 2
    assert "PHD hook blocked the tool: guard exited" in result.stderr
    assert "pre_tool hooks fail closed" in result.stderr


def test_shim_pre_tool_guard_exit_1_fails_closed(tmp_path):
    cmd = f"{Path(sys.executable).as_posix()} -c \"import sys; sys.exit(1)\""
    result = _run_shim(tmp_path, "pre_tool", cmd)
    assert result.returncode == 2
    assert "guard exited 1 (pre_tool hooks fail closed)" in result.stderr


def test_shim_pre_tool_guard_exit_2_has_no_fail_closed_prefix(tmp_path):
    cmd = f"{Path(sys.executable).as_posix()} -c \"import sys; sys.exit(2)\""
    result = _run_shim(tmp_path, "pre_tool", cmd)
    assert result.returncode == 2 and "fail closed" not in result.stderr


def test_shim_post_tool_exit_1_stays_1(tmp_path):
    cmd = f"{Path(sys.executable).as_posix()} -c \"import sys; sys.exit(1)\""
    assert _run_shim(tmp_path, "post_tool", cmd).returncode == 1
