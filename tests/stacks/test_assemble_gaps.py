import json
import shutil
from pathlib import Path

import pytest

from conftest import FIXTURES
from phd.errors import StackFetchError, StackInvalid
from phd.spec import Stack
from phd.stacks.assemble import assemble
from phd.stacks.manifest import compose


@pytest.fixture
def base(tmp_path):
    root = tmp_path / "base"
    shutil.copytree(FIXTURES / "stack", root)
    return root


def build(tmp_path, manifest):
    staging = tmp_path / "staging"
    return staging / "config", assemble(manifest, staging)


def minimal_base(tmp_path, name="min"):
    root = tmp_path / name
    root.mkdir()
    (root / "stack.json").write_text(json.dumps({"name": "min", "version": "1"}), encoding="utf-8")
    return root


def test_harnesses_and_default_models_are_written_to_stack_json(tmp_path, base):
    config, _ = build(tmp_path, compose("c", "1", base=base, harnesses=["claude", "codex"],
                                        default_models={"claude": "haiku"}))
    meta = json.loads((config / "stack.json").read_text(encoding="utf-8"))
    assert meta["harnesses"] == ["claude", "codex"] and meta["default_models"] == {"claude": "haiku"}


def test_base_without_mcp_or_hooks_files_assembles_cleanly(tmp_path):
    config, prov = build(tmp_path, compose("c", "1", base=minimal_base(tmp_path)))
    assert [p.kind for p in prov] == ["base"]
    assert not (config / ".mcp.json").exists() and not (config / "hooks").exists()


def test_base_hooks_false_without_a_hooks_dir_is_fine(tmp_path):
    config, _ = build(tmp_path, compose("c", "1", base=minimal_base(tmp_path), base_hooks=False))
    assert not (config / "hooks").exists()


def test_stack_without_base(tmp_path):
    config, _ = build(tmp_path, compose("c", "1", base_hooks=False))
    assert json.loads((config / "stack.json").read_text(encoding="utf-8")) == {"name": "c", "version": "1"}


def test_agent_file_must_be_markdown(tmp_path, base):
    txt = tmp_path / "agent.txt"
    txt.write_text("x", encoding="utf-8")
    with pytest.raises(StackInvalid, match=r"agent: .*agent\.txt is not a \.md file"):
        build(tmp_path, compose("c", "1", base=base, agent=txt))


def test_agent_folder_with_one_md_is_used(tmp_path, base):
    folder = tmp_path / "agent-folder"
    folder.mkdir()
    (folder / "boss.md").write_text("# boss\n", encoding="utf-8")
    (folder / "notes.txt").write_text("ignored", encoding="utf-8")
    config, prov = build(tmp_path, compose("c", "1", base=base, agent=folder))
    assert [p.name for p in prov if p.kind == "agent"] == ["boss"]
    assert [p.name for p in (config / "agents").glob("*.md")] == ["boss.md"]


def test_agent_copy_failure_is_stack_fetch_error(tmp_path, base, monkeypatch):
    md = tmp_path / "a.md"
    md.write_text("# a\n", encoding="utf-8")

    def deny(*a, **kw):
        raise PermissionError("secret path")
    monkeypatch.setattr(shutil, "copy2", deny)
    with pytest.raises(StackFetchError, match=r"agent: cannot copy the source into the stack \(PermissionError\)") as e:
        build(tmp_path, compose("c", "1", base=base, agent=md))
    assert "secret path" not in str(e.value)


def test_mcp_json_file_with_a_safe_name_is_named_after_itself(tmp_path, base):
    f = tmp_path / "weather.json"
    f.write_text(json.dumps({"mcpServers": {"weather": {"command": "w"}}}), encoding="utf-8")
    config, prov = build(tmp_path, compose("c", "1", base=base, mcp=[f]))
    assert [p.name for p in prov if p.kind == "mcp"] == ["weather"]
    assert Stack.load(config).mcp_servers["weather"] == {"command": "w"}
    assert not (config / "mcp" / "weather").exists()


def test_mcp_folder_needs_an_mcp_json(tmp_path, base):
    empty = tmp_path / "empty-mcp"
    empty.mkdir()
    with pytest.raises(StackFetchError, match=r"mcp\[0\]: \.mcp\.json not found in .*empty-mcp"):
        build(tmp_path, compose("c", "1", base=base, mcp=[empty]))


def test_mcp_servers_must_be_an_object(tmp_path, base):
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({"mcpServers": ["x"]}), encoding="utf-8")
    with pytest.raises(StackInvalid, match=r"mcp\[0\]: 'mcpServers' must be an object"):
        build(tmp_path, compose("c", "1", base=base, mcp=[f]))


def test_mcp_url_json_named_dot_mcp_takes_its_parent_folder_name(tmp_path, base, http_server):
    (http_server.www / "weather").mkdir()
    (http_server.www / "weather" / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"w": {"command": "w"}}}), encoding="utf-8")
    config, prov = build(tmp_path, compose("c", "1", base=base, mcp=[f"{http_server.url}/weather/.mcp.json"]))
    assert [p.name for p in prov if p.kind == "mcp"] == ["weather"]
    assert Stack.load(config).mcp_servers["w"] == {"command": "w"}


def test_hooks_must_be_an_array(tmp_path, base):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "hooks.json").write_text(json.dumps({"hooks": {"event": "x"}}), encoding="utf-8")
    with pytest.raises(StackInvalid, match=r"hooks\[0\]: 'hooks' must be an array"):
        build(tmp_path, compose("c", "1", base=base, hooks=[bundle]))


def test_skill_name_falls_back_to_the_folder_without_a_usable_frontmatter_name(tmp_path, base):
    for folder, text in (("closed-early", "---\ndescription: d\n---\nname: late\n"),
                         ("unclosed", "---\ndescription: d\nnot a key line\n")):
        d = tmp_path / folder
        d.mkdir()
        (d / "SKILL.md").write_text(text, encoding="utf-8")
    config, prov = build(tmp_path, compose("c", "1", base=base, skills=[tmp_path / "closed-early",
                                                                       tmp_path / "unclosed"]))
    assert [p.name for p in prov if p.kind == "skill"] == ["closed-early", "unclosed"]
    assert (config / "skills" / "closed-early" / "SKILL.md").is_file()


def test_unreadable_skill_md_is_stack_fetch_error(tmp_path, base, monkeypatch):
    d = tmp_path / "locked"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: locked\n---\n", encoding="utf-8")
    real = Path.read_text

    def read_text(self, *a, **kw):
        if self.name == "SKILL.md":
            raise PermissionError("secret path")
        return real(self, *a, **kw)
    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(StackFetchError, match=r"skills\[0\]: cannot read SKILL\.md \(PermissionError\)") as e:
        build(tmp_path, compose("c", "1", base=base, skills=[d]))
    assert "secret path" not in str(e.value)


def test_base_stack_json_must_be_json(tmp_path):
    root = tmp_path / "broken"
    root.mkdir()
    (root / "stack.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(StackInvalid, match="base stack.json: stack.json is not valid JSON"):
        build(tmp_path, compose("c", "1", base=root))


def test_base_stack_json_must_be_an_object(tmp_path):
    root = tmp_path / "listy"
    root.mkdir()
    (root / "stack.json").write_text("[1]", encoding="utf-8")
    with pytest.raises(StackInvalid, match="base stack.json: stack.json must be a JSON object"):
        build(tmp_path, compose("c", "1", base=root))
