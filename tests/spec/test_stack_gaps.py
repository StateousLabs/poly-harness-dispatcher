"""Validation edges of Stack.load / hash_tree / placeholders / Credentials not covered by test_stack.py."""

import json
import shutil

import pytest

from conftest import FIXTURES
from phd.errors import MissingCredentials, StackInvalid
from phd.spec import Credentials, Stack, hash_tree, placeholders


@pytest.fixture
def stack_copy(tmp_path):
    dst = tmp_path / "config"
    shutil.copytree(FIXTURES / "stack", dst)
    return dst


def _edit_meta(root, **changes):
    path = root / "stack.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.update(changes)
    path.write_text(json.dumps(meta), encoding="utf-8")


@pytest.mark.parametrize("bad", [[], ["gemini"], "claude", ["claude", "nope"]])
def test_harnesses_must_be_a_non_empty_list_of_known_harnesses(stack_copy, bad):
    _edit_meta(stack_copy, harnesses=bad)
    with pytest.raises(StackInvalid, match="'harnesses' must be a non-empty list"):
        Stack.load(stack_copy)


def test_default_models_must_be_an_object(stack_copy):
    _edit_meta(stack_copy, default_models=["haiku"])
    with pytest.raises(StackInvalid, match="'default_models' must be an object"):
        Stack.load(stack_copy)


def test_stack_without_skills_folder_loads_with_no_skills(stack_copy):
    shutil.rmtree(stack_copy / "skills")
    assert Stack.load(stack_copy).skills == ()


def test_pyc_files_are_not_hashed(stack_copy):
    before = hash_tree(stack_copy)
    (stack_copy / "hooks" / "stale.pyc").write_bytes(b"\x00junk")
    assert hash_tree(stack_copy) == before


def test_mcp_json_top_level_must_be_an_object(stack_copy):
    (stack_copy / ".mcp.json").write_text("[]", encoding="utf-8")
    with pytest.raises(StackInvalid, match="top level must be a JSON object"):
        Stack.load(stack_copy)


@pytest.mark.parametrize("server, match", [
    ("just-a-string", "must be an object"),
    ({"type": "stdio"}, "stdio server needs a 'command' string"),
    ({"command": 5}, "stdio server needs a 'command' string"),
    ({"type": "http"}, "http server needs a 'url' string"),
    ({"type": "http", "url": 5}, "http server needs a 'url' string"),
    ({"type": "sse", "url": "x"}, "unknown type 'sse'"),
])
def test_invalid_mcp_servers(stack_copy, server, match):
    (stack_copy / ".mcp.json").write_text(json.dumps({"mcpServers": {"bad": server}}), encoding="utf-8")
    with pytest.raises(StackInvalid, match=match):
        Stack.load(stack_copy)


def test_valid_http_mcp_server_is_accepted(stack_copy):
    server = {"type": "http", "url": "https://example.invalid/mcp"}
    (stack_copy / ".mcp.json").write_text(json.dumps({"mcpServers": {"remote": server}}), encoding="utf-8")
    assert Stack.load(stack_copy).mcp_servers == {"remote": server}


@pytest.mark.parametrize("hook", [{"event": "pre_tool"}, "echo hi"])
def test_hook_entry_needs_a_command_object(stack_copy, hook):
    (stack_copy / "hooks" / "hooks.json").write_text(json.dumps({"hooks": [hook]}), encoding="utf-8")
    with pytest.raises(StackInvalid, match="every hook needs a 'command' string"):
        Stack.load(stack_copy)


def test_env_refs_of_a_non_string_scalar_is_empty():
    assert placeholders.env_refs(5) == set()
    assert placeholders.env_refs(None) == set()


def test_codex_auth_file_missing(tmp_path):
    with pytest.raises(MissingCredentials, match="codex login"):
        Credentials.codex_auth_file(tmp_path / "absent.json")


def test_opencode_auth_file_missing(tmp_path):
    with pytest.raises(MissingCredentials, match="opencode auth login"):
        Credentials.opencode_auth_file(tmp_path / "absent.json")


@pytest.mark.parametrize("harness, constant, kind", [
    ("claude", "CLAUDE_HOST_CREDENTIALS", "claude_host_login"),
    ("codex", "CODEX_HOST_AUTH", "codex_auth_file"),
    ("opencode", "OPENCODE_HOST_AUTH", "opencode_auth_file"),
])
def test_from_host_uses_the_default_login_location(tmp_path, monkeypatch, harness, constant, kind):
    f = tmp_path / "auth.json"
    f.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("phd.spec.credentials." + constant, f)
    cred = Credentials.from_host(harness)
    assert (cred.harness, cred.kind, cred.path) == (harness, kind, f)


@pytest.mark.parametrize("key, bad", [("name", ""), ("name", 5), ("version", "  "), ("version", None)])
def test_name_and_version_must_be_non_empty_strings(stack_copy, key, bad):
    _edit_meta(stack_copy, **{key: bad})
    with pytest.raises(StackInvalid, match=f"{key!r} must be a non-empty string"):
        Stack.load(stack_copy)


def test_stack_without_harnesses_key_supports_every_harness(stack_copy):
    path = stack_copy / "stack.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    del meta["harnesses"]
    path.write_text(json.dumps(meta), encoding="utf-8")
    stack = Stack.load(stack_copy)
    assert stack.harnesses is None
    for harness in ("claude", "codex", "opencode"):
        stack.check_harness(harness)
