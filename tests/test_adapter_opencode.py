import json
import shutil

import pytest

from conftest import FIXTURES
from phd.harnesses import get_adapter
from phd.errors import HookUnsupported, UnsupportedFeature
from phd.materialize import materialize
from phd.spec import Stack


def test_opencode_home_layout_env_and_argv(tmp_path, make_spec, inputs, workdir):
    m = materialize(make_spec("opencode"), inputs, run_id="r1", run_dir=tmp_path / "r1")
    home, cfg_dir = m.home, m.home / "config"
    assert (cfg_dir / "skills" / "demo-notes" / "SKILL.md").is_file()
    assert "HELPER-IDENTITY-OK" in (home / "AGENTS.md").read_text(encoding="utf-8")

    cfg = json.loads((cfg_dir / "opencode.json").read_text(encoding="utf-8"))
    assert cfg["instructions"] == [(home / "AGENTS.md").as_posix()]
    assert cfg["mcp"]["echo"] == {"type": "local", "enabled": True, "environment": {"ECHO_PREFIX": "phd-echo"},
                                  "command": ["python", (home / "stack" / "mcp" / "echo_server.py").as_posix()]}
    assert cfg["permission"] == {"edit": "allow", "bash": {"*": "deny"}, "external_directory": "deny"}
    assert cfg["autoupdate"] is False
    plugin = (cfg_dir / "plugin" / "phd-hooks.js").read_text(encoding="utf-8")
    assert "tool.execute.before" in plugin and "hook_shim.py" in plugin

    assert m.argv == ["opencode", "run", "--format", "json", "-m", "opencode/big-pickle", "--dir", str(workdir)]
    env = m.env
    assert env["OPENCODE_CONFIG"] == str(cfg_dir / "opencode.json")
    assert env["OPENCODE_CONFIG_DIR"] == str(cfg_dir)
    assert env["XDG_DATA_HOME"] == str(home / "data")
    for flag in ("OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_EXTERNAL_SKILLS",
                 "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_PROJECT_CONFIG"):
        assert env[flag] == "1", flag
    assert json.loads(env["OPENCODE_AUTH_CONTENT"]) == {"opencode": {"type": "api", "key": "oc-test"}}


def test_opencode_full_modes_and_user_permission_merge(tmp_path, make_spec, inputs):
    full = materialize(make_spec("opencode", project_config="full", autonomy="full", unsafe=True),
                       inputs, run_id="a", run_dir=tmp_path / "a")
    assert "OPENCODE_DISABLE_PROJECT_CONFIG" not in full.env and full.argv[-1] == "--auto"
    assert json.loads((full.home / "config" / "opencode.json").read_text(encoding="utf-8"))["permission"] == {"*": "allow"}

    allow_echo = materialize(make_spec("opencode", harness_config={"permission": {"bash": {"echo *": "allow"}}}),
                             inputs, run_id="b", run_dir=tmp_path / "b")
    perm = json.loads((allow_echo.home / "config" / "opencode.json").read_text(encoding="utf-8"))["permission"]
    assert perm["bash"] == {"*": "deny", "echo *": "allow"}


def test_opencode_rejects_output_schema_and_session_hooks(tmp_path, make_spec):
    with pytest.raises(UnsupportedFeature, match="output_schema"):
        get_adapter("opencode").validate(make_spec("opencode", output_schema={"type": "object"}))
    stack_dir = tmp_path / "stack"
    shutil.copytree(FIXTURES / "stack", stack_dir)
    (stack_dir / "hooks" / "hooks.json").write_text(json.dumps({"hooks": [{"event": "session_start", "command": "x"}]}))
    with pytest.raises(HookUnsupported, match="session_start"):
        get_adapter("opencode").validate(make_spec("opencode", stack=Stack.load(stack_dir)))


def test_opencode_resume_argv(make_spec):
    assert get_adapter("opencode").resume_argv(["opencode", "run"], "ses_1", make_spec("opencode")) == \
        ["opencode", "run", "--session", "ses_1"]
