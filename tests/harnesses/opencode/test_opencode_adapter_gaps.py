import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from helpers import materialize_spec

from phd.errors import MissingCredentials
from phd.harnesses import get_adapter
from phd.harnesses.opencode.parser import OpencodeParser


def test_http_mcp_server_becomes_a_remote_entry(tmp_path, make_spec, inputs, stack):
    stack = replace(stack, mcp_servers={"web": {"type": "http", "url": "https://mcp.example/x",
                                               "headers": {"X-Key": "k"}},
                                        "bare": {"type": "http", "url": "https://mcp.example/y"}})
    m = materialize_spec(make_spec("opencode", stack=stack), inputs, run_id="r", run_dir=tmp_path / "r")
    mcp = json.loads((m.home / "config" / "opencode.json").read_text(encoding="utf-8"))["mcp"]
    assert mcp["web"] == {"type": "remote", "url": "https://mcp.example/x", "headers": {"X-Key": "k"}, "enabled": True}
    assert mcp["bare"]["headers"] == {} and mcp["bare"]["type"] == "remote"


def test_opencode_refuses_credentials_of_another_harness(credentials, tmp_path):
    spec = SimpleNamespace(credentials=credentials["claude"])
    with pytest.raises(MissingCredentials, match="claude_oauth_token"):
        get_adapter("opencode").credential_env(spec, tmp_path)


def test_opencode_answer_is_already_in_the_callers_shape(make_spec):
    adapter = get_adapter("opencode")
    answer = {"outcome": [1], "x": None}
    assert adapter.restore_answer(make_spec("opencode", output_schema={"type": "array"}), answer) is answer
    assert isinstance(adapter.parser(), OpencodeParser)


def test_stack_without_hooks_writes_no_plugin(tmp_path, make_spec, inputs, stack):
    m = materialize_spec(make_spec("opencode", stack=replace(stack, hooks=())), inputs, run_id="r",
                         run_dir=tmp_path / "r")
    assert not (m.home / "config" / "plugin").exists()
