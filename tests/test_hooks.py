import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from phd import hooks
from phd._shell import shell_join
from phd.errors import HookUnsupported
from phd.stack import HookDef

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


def test_claude_style_block(tmp_path):
    shim = tmp_path / "home" / "phd" / "hook_shim.py"
    block = hooks.claude_style_block("codex", [{"event": "pre_tool", "tool": "shell", "command": "c", "timeout_s": 10.0}], shim)
    entry = block["PreToolUse"][0]
    assert entry["matcher"] == "Bash"
    assert entry["hooks"][0]["type"] == "command" and entry["hooks"][0]["timeout"] == 10.0
    assert entry["hooks"][0]["command"].endswith(f"{shim.as_posix()} codex 0") or " " in shim.as_posix()


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
