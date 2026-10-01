import json

import pytest
from helpers import materialize_spec

from phd.constants import REDACTED
from phd.errors import UnknownHarness
from phd.harnesses import get_adapter
from phd.spec import Credentials


def test_claude_home_layout_and_argv(tmp_path, make_spec, inputs, workdir):
    spec = make_spec("claude", budget_usd=2.0)
    m = materialize_spec(spec, inputs, run_id="r1", run_dir=tmp_path / "r1")
    home = m.home
    assert home == tmp_path / "r1" / "home"
    assert (home / "skills" / "demo-notes" / "references" / "notes.md").is_file()
    assert (home / "skills" / "demo-notes" / "scripts" / "hello.py").is_file()
    assert (home / "skills" / "demo-minimal" / "SKILL.md").is_file()
    assert (home / "stack" / "stack.json").is_file()

    system = (home / "system-prompt.md").read_text(encoding="utf-8")
    assert "HELPER-IDENTITY-OK" in system and "## Project rules (binding)" in system
    assert "PINEAPPLE" not in system

    echo = json.loads((home / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["echo"]
    assert echo["args"] == [(home / "stack" / "mcp" / "echo_server.py").as_posix()]
    assert echo["env"] == {"ECHO_PREFIX": "phd-echo"}

    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    pre = settings["hooks"]["PreToolUse"][0]
    assert pre["matcher"] == "Bash|PowerShell" and pre["hooks"][0]["command"].endswith("claude 0")
    assert "Skill" in settings["permissions"]["allow"] and "mcp__echo" in settings["permissions"]["allow"]
    assert (home / "phd" / "hook_shim.py").is_file()

    a = m.argv
    assert a[:5] == ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    assert a[a.index("--model") + 1] == "haiku"
    assert a[a.index("--append-system-prompt-file") + 1] == str(home / "system-prompt.md")
    assert a[a.index("--mcp-config") + 1] == str(home / "mcp.json") and "--strict-mcp-config" in a
    assert a[a.index("--setting-sources") + 1] == "user"
    assert a[a.index("--permission-mode") + 1] == "acceptEdits"
    assert a[a.index("--max-budget-usd") + 1] == "2.0"

    assert m.env == {"CLAUDE_CONFIG_DIR": str(home), "DISABLE_AUTOUPDATER": "1",
                     "CLAUDE_CODE_OAUTH_TOKEN": "test-oauth-token"}
    assert m.redacted_env()["CLAUDE_CODE_OAUTH_TOKEN"] == REDACTED
    assert m.stdin.startswith("# Task brief") and "café" in m.stdin


def test_claude_full_modes(tmp_path, make_spec, inputs):
    m = materialize_spec(make_spec("claude", project_config="full", autonomy="full", unsafe=True,
                              output_schema={"type": "object"}), inputs, run_id="r", run_dir=tmp_path / "r")
    assert m.argv[m.argv.index("--setting-sources") + 1] == "user,project,local"
    assert "--dangerously-skip-permissions" in m.argv and "--permission-mode" not in m.argv
    assert json.loads(m.argv[m.argv.index("--json-schema") + 1]) == {"type": "object"}


def test_harness_config_merge_and_conflict_warning(tmp_path, make_spec, inputs):
    spec = make_spec("claude", harness_config={"permissions": {"allow": ["Bash(echo:*)"]}, "env": {"X": "1"}})
    m = materialize_spec(spec, inputs, run_id="r", run_dir=tmp_path / "r")
    settings = json.loads((m.home / "settings.json").read_text(encoding="utf-8"))
    assert settings["permissions"]["allow"][-1] == "Bash(echo:*)" and settings["env"] == {"X": "1"}

    clash = make_spec("claude", harness_config={"permissions": {"allow": "Bash"}})
    m2 = materialize_spec(clash, inputs, run_id="r2", run_dir=tmp_path / "r2")
    assert any("permissions.allow" in w for w in m2.warnings)


def test_host_login_injects_access_token_only(tmp_path, make_spec, inputs):
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "at-123", "refreshToken": "rt-SECRET"}}))
    m = materialize_spec(make_spec("claude", credentials=Credentials.claude_host_login(creds)), inputs,
                    run_id="r", run_dir=tmp_path / "r")
    assert m.env["CLAUDE_CODE_OAUTH_TOKEN"] == "at-123"
    for f in m.home.rglob("*"):
        if f.is_file():
            assert b"rt-SECRET" not in f.read_bytes(), f


def test_claude_resume_argv(make_spec):
    adapter = get_adapter("claude")
    assert adapter.resume_argv(["claude", "-p"], "sid-1", make_spec("claude")) == ["claude", "-p", "--resume", "sid-1"]


def test_get_adapter_unknown():
    with pytest.raises(UnknownHarness):
        get_adapter("gemini")
