import dataclasses
import hashlib
import json
import shutil

import pytest

from conftest import FIXTURES
from phd.errors import HookUnsupported, StackIncompatible, StackInvalid
from phd.spec import HookDef, Stack, StoreEntry, hash_tree


@pytest.fixture
def stack_copy(tmp_path):
    dst = tmp_path / "config"
    shutil.copytree(FIXTURES / "stack", dst)
    return dst


def test_load_reads_every_part(stack):
    assert stack.ref == "fixture-helper@0.1.0"
    assert stack.harnesses == ("claude", "codex", "opencode")
    assert stack.agent_name == "helper"
    assert "HELPER-IDENTITY-OK" in stack.identity
    assert [s.name for s in stack.skills] == ["demo-minimal", "demo-notes"]
    assert stack.mcp_servers["echo"]["args"] == ["${stack}/mcp/echo_server.py"]
    assert stack.hooks == (HookDef("pre_tool", "python ${stack}/hooks/guard.py", "shell", 10.0),)


def test_requires_exactly_one_agent(stack_copy):
    (stack_copy / "agents" / "second.md").write_text("# two", encoding="utf-8")
    with pytest.raises(StackInvalid, match="exactly one agent"):
        Stack.load(stack_copy)


def test_missing_stack_json(stack_copy):
    (stack_copy / "stack.json").unlink()
    with pytest.raises(StackInvalid, match="stack.json"):
        Stack.load(stack_copy)


def test_skill_without_skill_md(stack_copy):
    (stack_copy / "skills" / "broken").mkdir()
    with pytest.raises(StackInvalid, match="no SKILL.md"):
        Stack.load(stack_copy)


def test_bad_mcp_type(stack_copy):
    (stack_copy / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": {"type": "sse", "url": "u"}}}))
    with pytest.raises(StackInvalid, match="unknown type 'sse'"):
        Stack.load(stack_copy)


def test_unknown_hook_event(stack_copy):
    (stack_copy / "hooks" / "hooks.json").write_text(json.dumps({"hooks": [{"event": "before_everything", "command": "x"}]}))
    with pytest.raises(HookUnsupported, match="before_everything"):
        Stack.load(stack_copy)


def test_invalid_json_names_the_file(stack_copy):
    (stack_copy / ".mcp.json").write_text("{not json")
    with pytest.raises(StackInvalid, match=r"\.mcp\.json"):
        Stack.load(stack_copy)


def test_harness_compatibility(stack_copy):
    meta = json.loads((stack_copy / "stack.json").read_text())
    meta["harnesses"] = ["claude"]
    (stack_copy / "stack.json").write_text(json.dumps(meta))
    s = Stack.load(stack_copy)
    s.check_harness("claude")
    with pytest.raises(StackIncompatible, match="'codex'"):
        s.check_harness("codex")


def test_content_hash_changes_with_content(stack_copy):
    before = Stack.load(stack_copy).content_hash()
    assert before == Stack.load(stack_copy).content_hash()
    (stack_copy / "skills" / "demo-notes" / "references" / "notes.md").write_text("changed")
    assert Stack.load(stack_copy).content_hash() != before


def test_content_hash_ignores_pycache_and_hidden(stack_copy):
    """Hash should not change when __pycache__ or hidden files are added."""
    before = Stack.load(stack_copy).content_hash()

    # Add __pycache__ directory with .pyc file
    pycache_dir = stack_copy / "hooks" / "__pycache__"
    pycache_dir.mkdir()
    (pycache_dir / "guard.cpython-313.pyc").write_bytes(b"fake pyc")

    # Add hidden .git directory
    git_dir = stack_copy / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main")

    # Hash should be unchanged
    after = Stack.load(stack_copy).content_hash()
    assert after == before


@pytest.mark.parametrize("invalid_mcp,expected_match", [
    (None, "'mcpServers' must be an object"),
    ([], "'mcpServers' must be an object"),
    ("bad", "'mcpServers' must be an object"),
])
def test_invalid_mcp_type(stack_copy, invalid_mcp, expected_match):
    """mcpServers must be a dict."""
    (stack_copy / ".mcp.json").write_text(json.dumps({"mcpServers": invalid_mcp}))
    with pytest.raises(StackInvalid, match=expected_match):
        Stack.load(stack_copy)


@pytest.mark.parametrize("invalid_hooks,expected_match", [
    (None, "'hooks' must be an array"),
    ({}, "'hooks' must be an array"),
    ("bad", "'hooks' must be an array"),
])
def test_invalid_hooks_type(stack_copy, invalid_hooks, expected_match):
    """hooks must be a list."""
    (stack_copy / "hooks" / "hooks.json").write_text(json.dumps({"hooks": invalid_hooks}))
    with pytest.raises(StackInvalid, match=expected_match):
        Stack.load(stack_copy)


@pytest.mark.parametrize("bad_match,expected_match", [
    ("shell", "'match' must be an object"),
    (123, "'match' must be an object"),
    ([], "'match' must be an object"),
])
def test_invalid_hook_match_type(stack_copy, bad_match, expected_match):
    """Hook match must be a dict if present."""
    (stack_copy / "hooks" / "hooks.json").write_text(json.dumps({
        "hooks": [{"event": "pre_tool", "command": "x", "match": bad_match}]
    }))
    with pytest.raises(StackInvalid, match=expected_match):
        Stack.load(stack_copy)


@pytest.mark.parametrize("bad_timeout,expected_match", [
    ("soon", "'timeout_s' must be numeric"),
    ([10], "'timeout_s' must be numeric"),
    ({}, "'timeout_s' must be numeric"),
])
def test_invalid_hook_timeout_type(stack_copy, bad_timeout, expected_match):
    """Hook timeout_s must be numeric."""
    (stack_copy / "hooks" / "hooks.json").write_text(json.dumps({
        "hooks": [{"event": "pre_tool", "command": "x", "timeout_s": bad_timeout}]
    }))
    with pytest.raises(StackInvalid, match=expected_match):
        Stack.load(stack_copy)


@pytest.mark.parametrize("bad_models,expected_match", [
    ([], "'default_models' must be an object"),
    ("bad", "'default_models' must be an object"),
    (None, "'default_models' must be an object"),
])
def test_invalid_default_models_type(stack_copy, bad_models, expected_match):
    """default_models must be a dict."""
    meta = json.loads((stack_copy / "stack.json").read_text())
    meta["default_models"] = bad_models
    (stack_copy / "stack.json").write_text(json.dumps(meta))
    with pytest.raises(StackInvalid, match=expected_match):
        Stack.load(stack_copy)


def test_invalid_default_models_values(stack_copy):
    """default_models values must be strings."""
    meta = json.loads((stack_copy / "stack.json").read_text())
    meta["default_models"] = {"claude": 123}
    (stack_copy / "stack.json").write_text(json.dumps(meta))
    with pytest.raises(StackInvalid, match="'default_models' values must be strings"):
        Stack.load(stack_copy)


def test_content_hash_under_hidden_directory(tmp_path):
    """content_hash works correctly even when stack is under a hidden directory.

    Regression test: ensure filtering only applies to relative paths, not absolute paths.
    If the stack is under ~/.phd or .superpowers, the hash must not collapse to empty.
    """
    # Copy fixture stack to a hidden directory
    hidden_root = tmp_path / ".hidden"
    hidden_root.mkdir()
    stack_root = hidden_root / "config"
    shutil.copytree(FIXTURES / "stack", stack_root)

    # Load and compute hash
    s = Stack.load(stack_root)
    hash1 = s.content_hash()

    # Verify hash is not the empty digest (would indicate all files were skipped)
    empty_digest = "sha256:" + hashlib.sha256().hexdigest()
    assert hash1 != empty_digest, "Hash collapsed to empty—filtering incorrectly applied to absolute path"

    # Verify hash is stable
    assert s.content_hash() == hash1

    # Verify hash changes when content changes
    (stack_root / "skills" / "demo-notes" / "references" / "notes.md").write_text("modified content")
    hash2 = Stack.load(stack_root).content_hash()
    assert hash2 != hash1, "Hash should change when content changes"


def test_hash_tree_matches_content_hash(stack):
    assert hash_tree(stack.root) == stack.content_hash()


def test_hash_tree_ignore_meta(stack_copy):
    a = hash_tree(stack_copy, ignore_meta=("name", "version", "description"))
    meta = json.loads((stack_copy / "stack.json").read_text())
    meta.update(name="renamed", version="9.9.9", description="other")
    (stack_copy / "stack.json").write_text(json.dumps(meta))
    assert hash_tree(stack_copy, ignore_meta=("name", "version", "description")) == a
    assert hash_tree(stack_copy) != hash_tree(stack_copy, ignore_meta=("name",))
    meta["harnesses"] = ["claude"]
    (stack_copy / "stack.json").write_text(json.dumps(meta))
    assert hash_tree(stack_copy, ignore_meta=("name", "version", "description")) != a


def test_store_entry_and_stack_store(stack, tmp_path):
    assert stack.store is None
    e = StoreEntry(ref="a@1", name="a", version="1", hash="sha256:x", created_at="t", manifest=None,
                   path=tmp_path, requested="b@2")
    assert e.reused
    assert not dataclasses.replace(e, requested="a@1").reused
    assert dataclasses.replace(stack, store=e).store is e
