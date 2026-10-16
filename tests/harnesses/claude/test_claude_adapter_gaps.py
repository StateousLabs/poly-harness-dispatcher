import json
from types import SimpleNamespace

import pytest
from helpers import materialize_spec

from phd.errors import MissingCredentials
from phd.harnesses import get_adapter
from phd.harnesses.claude.parser import ClaudeParser
from phd.spec import Credentials


def test_anthropic_api_key_goes_in_the_environment(tmp_path, make_spec, inputs):
    spec = make_spec("claude", credentials=Credentials.anthropic_api_key("sk-test"))
    m = materialize_spec(spec, inputs, run_id="r", run_dir=tmp_path / "r")
    assert m.env["ANTHROPIC_API_KEY"] == "sk-test" and "CLAUDE_CODE_OAUTH_TOKEN" not in m.env


def test_claude_refuses_credentials_of_another_harness(credentials, tmp_path):
    spec = SimpleNamespace(credentials=credentials["codex"])
    with pytest.raises(MissingCredentials, match="codex_auth_file"):
        get_adapter("claude").credential_env(spec, tmp_path)


def test_claude_owns_its_files_and_keeps_no_secrets_on_disk(tmp_path):
    adapter = get_adapter("claude")
    assert [p.name for p in adapter.owned_paths(tmp_path)] == ["skills", "system-prompt.md", "mcp.json", "settings.json"]
    assert adapter.secret_paths(tmp_path) == []
    assert isinstance(adapter.parser(), ClaudeParser)


def test_claude_without_hooks_writes_no_hook_settings(tmp_path, make_spec, inputs, stack):
    from dataclasses import replace
    spec = make_spec("claude", stack=replace(stack, hooks=()))
    m = materialize_spec(spec, inputs, run_id="r", run_dir=tmp_path / "r")
    assert "hooks" not in json.loads((m.home / "settings.json").read_text(encoding="utf-8"))


def test_claude_restore_answer_unwraps_only_wrapped_schemas(make_spec):
    adapter = get_adapter("claude")
    wrapped = make_spec("claude", output_schema={"type": "array", "items": {"type": "string"}})
    assert adapter.restore_answer(wrapped, {"outcome": ["a"]}) == ["a"]
    obj = make_spec("claude", output_schema={"type": "object"})
    assert adapter.restore_answer(obj, {"outcome": 1}) == {"outcome": 1}  # an object root is never wrapped
    assert adapter.restore_answer(make_spec("claude"), {"x": 1}) == {"x": 1}
