import json
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import materialize_spec

from phd.errors import MissingCredentials
from phd.harnesses import get_adapter
from phd.harnesses.codex import adapter as codex_adapter
from phd.harnesses.codex.parser import CodexParser


@pytest.fixture
def empty_user_home(tmp_path, monkeypatch):
    home = tmp_path / "empty-userhome"
    home.mkdir()
    monkeypatch.setattr(codex_adapter, "user_home", lambda: home)
    return home


def _config(m):
    return tomllib.loads((m.home / "config.toml").read_text(encoding="utf-8"))


def test_user_home_is_the_os_home():
    assert codex_adapter.user_home() == Path.home()


def test_validate_warns_about_budget_and_http_mcp(make_spec, stack):
    adapter = get_adapter("codex")
    assert adapter.validate(make_spec("codex")) == []
    http = replace(stack, mcp_servers={"web": {"type": "http", "url": "https://mcp.example/x"}})
    warnings = adapter.validate(make_spec("codex", stack=http, budget_usd=1.0))
    assert len(warnings) == 2
    assert "budget_usd cannot be enforced" in warnings[0] and "http MCP translation" in warnings[1]


@pytest.mark.parametrize("headers, expected", [
    ({"Authorization": "Bearer t"}, {"Authorization": "Bearer t"}), (None, None)])
def test_http_mcp_server_translates_to_url_and_headers(tmp_path, make_spec, inputs, stack, empty_user_home, headers, expected):
    server = {"type": "http", "url": "https://mcp.example/x"}
    if headers:
        server["headers"] = headers
    spec = make_spec("codex", stack=replace(stack, mcp_servers={"web": server}))
    m = materialize_spec(spec, inputs, run_id="r", run_dir=tmp_path / "r")
    entry = _config(m)["mcp_servers"]["web"]
    assert entry["url"] == "https://mcp.example/x" and entry["default_tools_approval_mode"] == "approve"
    assert entry.get("http_headers") == expected and "command" not in entry


def test_minimal_stack_writes_no_mcp_hooks_or_skill_sections(tmp_path, make_spec, inputs, stack, empty_user_home):
    spec = make_spec("codex", stack=replace(stack, mcp_servers={}, hooks=()), harness_builtins=True,
                     project_config="full")
    m = materialize_spec(spec, inputs, run_id="r", run_dir=tmp_path / "r")
    cfg = _config(m)
    assert "mcp_servers" not in cfg and "skills" not in cfg and "project_doc_max_bytes" not in cfg
    assert not (m.home / "hooks.json").exists()
    assert "--ignore-rules" not in m.argv and "--dangerously-bypass-hook-trust" not in m.argv


def test_codex_refuses_credentials_of_another_harness(credentials, tmp_path):
    spec = SimpleNamespace(credentials=credentials["claude"])
    with pytest.raises(MissingCredentials, match="claude_oauth_token"):
        get_adapter("codex").credential_env(spec, tmp_path)
    assert not (tmp_path / "auth.json").exists()


def test_owned_and_secret_paths(tmp_path):
    adapter = get_adapter("codex")
    names = [p.name for p in adapter.owned_paths(tmp_path)]
    assert names == ["skills", "AGENTS.md", "config.toml", "hooks.json", "output-schema.json", "auth.json"]
    assert adapter.secret_paths(tmp_path) == [tmp_path / "auth.json"]
    assert isinstance(adapter.parser(), CodexParser)


def test_resume_without_schema_keeps_only_the_resume_flags(make_spec):
    spec = make_spec("codex")
    fresh = ["codex", "exec", "--json", "--strict-config", "-C", "/w", "-m", "x", "--ignore-rules", "-"]
    argv = get_adapter("codex").resume_argv(fresh, "sid", spec)
    assert argv == ["codex", "exec", "resume", "sid", "--json", "--strict-config", "--ignore-rules",
                    "-m", spec.model, "-"]


def test_resume_keeps_the_output_schema_flag_and_path(make_spec):
    fresh = ["codex", "exec", "--json", "--output-schema", "/h/output-schema.json", "-"]
    argv = get_adapter("codex").resume_argv(fresh, "sid", make_spec("codex"))
    assert argv[4:7] == ["--json", "--output-schema", "/h/output-schema.json"]


def test_restore_answer_unwraps_and_strips_by_the_callers_schema(make_spec):
    adapter = get_adapter("codex")
    assert adapter.restore_answer(make_spec("codex"), {"a": None}) == {"a": None}
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["a"]}
    assert adapter.restore_answer(make_spec("codex", output_schema=schema), {"a": "x", "b": None}) == {"a": "x"}
    listed = make_spec("codex", output_schema={"type": "array", "items": {"type": "string"}})
    assert adapter.restore_answer(listed, {"outcome": ["q"]}) == ["q"]


def test_output_schema_file_is_strict_and_passed_on_the_command_line(tmp_path, make_spec, inputs, empty_user_home):
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    m = materialize_spec(make_spec("codex", output_schema=schema), inputs, run_id="r", run_dir=tmp_path / "r")
    written = json.loads((m.home / "output-schema.json").read_text(encoding="utf-8"))
    assert written["required"] == ["a"] and written["additionalProperties"] is False
    assert m.argv[m.argv.index("--output-schema") + 1] == str(m.home / "output-schema.json")


def test_stdio_mcp_server_without_env_has_no_env_table(tmp_path, make_spec, inputs, stack, empty_user_home):
    server = {"command": "python", "args": ["srv.py"]}
    spec = make_spec("codex", stack=replace(stack, mcp_servers={"plain": server}))
    m = materialize_spec(spec, inputs, run_id="r", run_dir=tmp_path / "r")
    entry = _config(m)["mcp_servers"]["plain"]
    assert entry == {"command": "python", "args": ["srv.py"], "default_tools_approval_mode": "approve"}
