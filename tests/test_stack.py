import json
import shutil

import pytest

from conftest import FIXTURES
from phd.errors import HookUnsupported, StackIncompatible, StackInvalid
from phd.stack import HookDef, Stack


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
