import json
import shutil

import pytest

from conftest import FIXTURES
from phd.errors import StackFetchError, StackInvalid
from phd.spec import Stack
from phd.stacks.assemble import assemble
from phd.stacks.manifest import compose


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "parts"
    shutil.copytree(FIXTURES / "stack", root / "base")
    skill = root / "extra-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: extra\ndescription: d\n---\nbody\n", encoding="utf-8")
    plain = root / "plain-skill"
    plain.mkdir()
    (plain / "SKILL.md").write_text("no frontmatter\n", encoding="utf-8")
    clash = root / "clash"
    clash.mkdir()
    (clash / "SKILL.md").write_text("---\nname: demo-notes\n---\nnew\n", encoding="utf-8")
    weather = root / "weather"
    weather.mkdir()
    (weather / ".mcp.json").write_text(json.dumps({"mcpServers": {"weather": {
        "command": "python", "args": ["${here}/server.py"]}}}), encoding="utf-8")
    (weather / "server.py").write_text("print(1)\n", encoding="utf-8")
    audit = root / "audit"
    audit.mkdir()
    (audit / "hooks.json").write_text(json.dumps({"hooks": [
        {"event": "post_tool", "command": "python ${here}/audit.py"}]}), encoding="utf-8")
    (audit / "audit.py").write_text("print(1)\n", encoding="utf-8")
    (root / "other.md").write_text("# Other agent\n", encoding="utf-8")
    return root


def build(tmp_path, manifest):
    staging = tmp_path / "staging"
    provenance = assemble(manifest, staging)
    return staging / "config", provenance


def test_full_assembly(tmp_path, parts):
    m = compose("composed", "2.0.0", base=parts / "base", agent=parts / "other.md",
                skills=[parts / "extra-skill", parts / "plain-skill"], mcp=[parts / "weather"],
                hooks=[parts / "audit"])
    config, provenance = build(tmp_path, m)
    stack = Stack.load(config)
    assert stack.ref == "composed@2.0.0" and stack.agent_name == "other"
    assert [s.name for s in stack.skills] == ["demo-minimal", "demo-notes", "extra", "plain-skill"]
    assert stack.mcp_servers["weather"]["args"] == ["${stack}/mcp/weather/server.py"]
    assert "echo" in stack.mcp_servers
    assert (config / "mcp" / "weather" / "server.py").is_file()
    commands = [h.command for h in stack.hooks]
    assert "python ${stack}/hooks/guard.py" in commands and "python ${stack}/hooks/audit/audit.py" in commands
    assert [p.kind for p in provenance] == ["base", "agent", "skill", "skill", "mcp", "hooks"]
    assert all(p.digest.startswith("sha256:") for p in provenance)


def test_skill_clash_needs_replace(tmp_path, parts):
    with pytest.raises(StackInvalid, match="skill 'demo-notes' already exists"):
        build(tmp_path, compose("c", "1", base=parts / "base", skills=[parts / "clash"]))
    config, _ = build(tmp_path / "ok", compose("c", "1", base=parts / "base",
                                               skills=[{"source": str(parts / "clash"), "replace": True}]))
    assert (config / "skills" / "demo-notes" / "SKILL.md").read_text(encoding="utf-8").endswith("new\n")


def test_mcp_server_clash_needs_replace(tmp_path, parts):
    echo = parts / "echo2"
    echo.mkdir()
    (echo / ".mcp.json").write_text(json.dumps({"mcpServers": {"echo": {"command": "node"}}}), encoding="utf-8")
    with pytest.raises(StackInvalid, match="MCP server 'echo' already exists"):
        build(tmp_path, compose("c", "1", base=parts / "base", mcp=[echo]))
    config, _ = build(tmp_path / "ok", compose("c", "1", base=parts / "base",
                                               mcp=[{"source": str(echo), "replace": True}]))
    assert Stack.load(config).mcp_servers["echo"]["command"] == "node"


def test_hook_bundle_clash_and_base_hooks_false(tmp_path, parts):
    with pytest.raises(StackInvalid, match="hook bundle 'audit' already exists"):
        build(tmp_path, compose("c", "1", base=parts / "base", hooks=[parts / "audit", parts / "audit"]))
    config, _ = build(tmp_path / "nobase", compose("c", "1", base=parts / "base", base_hooks=False,
                                                   hooks=[parts / "audit"]))
    assert [h.command for h in Stack.load(config).hooks] == ["python ${stack}/hooks/audit/audit.py"]
    assert not (config / "hooks" / "guard.py").exists()


def test_metadata_from_manifest_and_inherited(tmp_path, parts):
    config, _ = build(tmp_path, compose("c", "1", base=parts / "base", description="mine"))
    meta = json.loads((config / "stack.json").read_text(encoding="utf-8"))
    assert meta["name"] == "c" and meta["version"] == "1" and meta["description"] == "mine"
    assert meta["harnesses"] == ["claude", "codex", "opencode"]  # inherited from the base


def test_without_base(tmp_path, parts):
    config, _ = build(tmp_path, compose("solo", "1", agent=parts / "other.md", skills=[parts / "extra-skill"]))
    stack = Stack.load(config)
    assert stack.ref == "solo@1" and [s.name for s in stack.skills] == ["extra"]


def test_missing_marker_file(tmp_path, parts):
    with pytest.raises(StackFetchError, match="SKILL.md not found"):
        build(tmp_path, compose("c", "1", base=parts / "base", skills=[parts / "weather"]))


def test_agent_folder_needs_exactly_one_md(tmp_path, parts):
    with pytest.raises(StackInvalid, match="exactly one .md"):
        build(tmp_path, compose("c", "1", base=parts / "base", agent=parts / "base"))
