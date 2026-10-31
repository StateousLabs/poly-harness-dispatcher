import json

import pytest
from helpers import materialize_spec

from phd._util.npm import npm_env
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
                     "CLAUDE_CODE_OAUTH_TOKEN": "test-oauth-token", **npm_env()}
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


def _login_file(tmp_path):
    login = tmp_path / ".credentials.json"
    login.write_text(json.dumps({"claudeAiOauth": {"accessToken": "at-123", "refreshToken": "rt-123"}}), encoding="utf-8")
    return login


def test_host_login_copies_the_whole_login_file(tmp_path, make_spec, inputs):
    login = _login_file(tmp_path)
    m = materialize_spec(make_spec("claude", credentials=Credentials.claude_host_login(login)), inputs,
                         run_id="r", run_dir=tmp_path / "r")
    assert (m.home / ".credentials.json").read_bytes() == login.read_bytes()  # verbatim, refresh token included
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in m.env and "ANTHROPIC_API_KEY" not in m.env
    adapter = get_adapter("claude")
    assert adapter.secret_paths(m.home) == [m.home / ".credentials.json"]
    assert m.home / ".credentials.json" in adapter.owned_paths(m.home)


def test_token_credentials_write_no_login_file(tmp_path, make_spec, inputs):
    m = materialize_spec(make_spec("claude"), inputs, run_id="r", run_dir=tmp_path / "r")
    assert m.env["CLAUDE_CODE_OAUTH_TOKEN"] == "test-oauth-token"
    assert not (m.home / ".credentials.json").exists()


def test_chrome_adds_the_flag_and_allows_the_chrome_tools(tmp_path, make_spec, inputs):
    spec = make_spec("claude", credentials=Credentials.claude_host_login(_login_file(tmp_path)), chrome=True)
    m = materialize_spec(spec, inputs, run_id="r", run_dir=tmp_path / "r")
    assert "--chrome" in m.argv
    allow = json.loads((m.home / "settings.json").read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "mcp__claude-in-chrome" in allow


def test_chrome_is_off_by_default(tmp_path, make_spec, inputs):
    m = materialize_spec(make_spec("claude"), inputs, run_id="r", run_dir=tmp_path / "r")
    assert "--chrome" not in m.argv
    allow = json.loads((m.home / "settings.json").read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "mcp__claude-in-chrome" not in allow


def test_chrome_is_kept_on_resume(tmp_path, make_spec):
    spec = make_spec("claude", credentials=Credentials.claude_host_login(_login_file(tmp_path)), chrome=True)
    adapter = get_adapter("claude")
    assert adapter.resume_argv(["claude", "-p", "--chrome"], "sid-1", spec) == ["claude", "-p", "--chrome", "--resume", "sid-1"]


def test_claude_resume_argv(make_spec):
    adapter = get_adapter("claude")
    assert adapter.resume_argv(["claude", "-p"], "sid-1", make_spec("claude")) == ["claude", "-p", "--resume", "sid-1"]


def test_get_adapter_unknown():
    with pytest.raises(UnknownHarness):
        get_adapter("gemini")


def _settings(tmp_path, make_spec, inputs, **kw):
    m = materialize_spec(make_spec("claude", **kw), inputs, run_id="r", run_dir=tmp_path / "r")
    return json.loads((m.home / "settings.json").read_text(encoding="utf-8"))


def test_claude_builtins_suppressed_by_default(tmp_path, make_spec, inputs):
    s = _settings(tmp_path, make_spec, inputs)
    assert s["disableBundledSkills"] is True
    assert s["enabledPlugins"] == {"cc-plugin-agents-md@builtin": False, "cc-plugin-telemetry@builtin": False,
                                   "cc-plugin-plugin-authoring@builtin": False}
    assert s["permissions"]["deny"] == ["Skill(design)", "Skill(doctor)"]
    assert "Skill" in s["permissions"]["allow"]


def test_claude_builtins_opt_in_writes_no_suppression(tmp_path, make_spec, inputs):
    s = _settings(tmp_path, make_spec, inputs, harness_builtins=True)
    assert "disableBundledSkills" not in s and "enabledPlugins" not in s
    assert "deny" not in s["permissions"]


def test_claude_builtins_harness_config_merges_on_top(tmp_path, make_spec, inputs):
    s = _settings(tmp_path, make_spec, inputs,
                  harness_config={"permissions": {"deny": ["Bash(rm:*)"]}, "enabledPlugins": {"x@y": True}})
    assert s["permissions"]["deny"] == ["Skill(design)", "Skill(doctor)", "Bash(rm:*)"]
    assert s["enabledPlugins"]["x@y"] is True and s["enabledPlugins"]["cc-plugin-telemetry@builtin"] is False
