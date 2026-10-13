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


def test_mcp_folder_replace_cleans_old_servers(tmp_path, parts):
    """When replacing an MCP folder, remove servers from the old folder."""
    # Create two sources with same component name "weather" but different servers
    x_dir = parts / "x"
    x_dir.mkdir()
    x_weather = x_dir / "weather"
    shutil.copytree(parts / "weather", x_weather)
    (x_weather / ".mcp.json").write_text(json.dumps({"mcpServers": {"a": {
        "command": "python", "args": ["${here}/a.py"]}}}), encoding="utf-8")

    y_dir = parts / "y"
    y_dir.mkdir()
    y_weather = y_dir / "weather"
    shutil.copytree(parts / "weather", y_weather)
    (y_weather / ".mcp.json").write_text(json.dumps({"mcpServers": {"b": {
        "command": "python", "args": ["${here}/b.py"]}}}), encoding="utf-8")

    config, _ = build(tmp_path, compose("c", "1", base=parts / "base",
                                        mcp=[x_weather, {"source": str(y_weather), "replace": True}]))
    stack = Stack.load(config)
    assert "b" in stack.mcp_servers and "a" not in stack.mcp_servers


def test_mcp_folder_clash_without_replace(tmp_path, parts):
    """Same folder name without replace raises StackInvalid."""
    with pytest.raises(StackInvalid, match="MCP component folder 'weather' already exists"):
        m = compose("c", "1", base=parts / "base", mcp=[parts / "weather", parts / "weather"])
        build(tmp_path, m)


def test_base_invalid_mcp_servers_type(tmp_path, parts):
    """Base with invalid mcpServers type raises StackInvalid."""
    bad_base = parts / "badbase"
    shutil.copytree(parts / "base", bad_base)
    mcp_file = bad_base / ".mcp.json"
    if mcp_file.is_file():
        data = json.loads(mcp_file.read_text(encoding="utf-8"))
        data["mcpServers"] = "not a dict"
        mcp_file.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(StackInvalid, match="base: 'mcpServers' must be an object"):
        build(tmp_path, compose("c", "1", base=bad_base))




def test_base_invalid_hooks_type(tmp_path, parts):
    """Base with invalid hooks type raises StackInvalid."""
    bad_base = parts / "badbase2"
    shutil.copytree(parts / "base", bad_base)
    hooks_file = bad_base / "hooks" / "hooks.json"
    if hooks_file.is_file():
        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        data["hooks"] = "not a list"
        hooks_file.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(StackInvalid, match="base: 'hooks' must be an array"):
        build(tmp_path, compose("c", "1", base=bad_base))




def test_base_here_rewritten_to_stack(tmp_path, parts):
    """Base's ${here} in .mcp.json and hooks.json is rewritten to ${stack}."""
    custom_base = parts / "custombase"
    shutil.copytree(parts / "base", custom_base)

    mcp_file = custom_base / ".mcp.json"
    data = json.loads(mcp_file.read_text(encoding="utf-8"))
    data["mcpServers"]["custom"] = {"command": "python", "args": ["${here}/test.py"]}
    mcp_file.write_text(json.dumps(data), encoding="utf-8")

    hooks_file = custom_base / "hooks" / "hooks.json"
    data = json.loads(hooks_file.read_text(encoding="utf-8"))
    data["hooks"].append({"event": "post_tool", "command": "python ${here}/test.py"})
    hooks_file.write_text(json.dumps(data), encoding="utf-8")

    config, _ = build(tmp_path, compose("c", "1", base=custom_base))
    stack = Stack.load(config)
    assert stack.mcp_servers["custom"]["args"] == ["${stack}/test.py"]
    commands = [h.command for h in stack.hooks]
    assert any("${stack}/test.py" in cmd for cmd in commands)




def test_skill_hostile_name(tmp_path, parts):
    """Skill with hostile name like ../evil raises StackInvalid."""
    evil = parts / "evil"
    evil.mkdir()
    (evil / "SKILL.md").write_text("---\nname: ../evil\n---\nbody\n", encoding="utf-8")

    with pytest.raises(StackInvalid, match="invalid skill name"):
        build(tmp_path, compose("c", "1", base=parts / "base", skills=[evil]))




@pytest.mark.parametrize("bad", ["con", "Aux", "nul.md", "com1", "name."])
def test_component_names_follow_the_portable_name_rule(tmp_path, parts, bad):
    """Windows device names and a trailing dot are refused on every OS, so stacks stay portable."""
    skill = parts / "dev-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(f"---\nname: {bad}\n---\nbody\n", encoding="utf-8")
    with pytest.raises(StackInvalid, match=r"skills\[0\]: invalid skill name .* not a Windows device name"):
        build(tmp_path, compose("c", "1", base=parts / "base", skills=[skill]))


@pytest.mark.parametrize("bad", ["con", "com1"])  # no trailing dot: Windows cannot create that folder
def test_hook_bundle_and_mcp_folder_names_follow_the_rule(tmp_path, parts, bad):
    hooks = parts / "bundles" / bad
    shutil.copytree(parts / "audit", hooks)
    with pytest.raises(StackInvalid, match=r"invalid hook bundle name .* not a Windows device name"):
        build(tmp_path, compose("c", "1", base=parts / "base", hooks=[hooks]))
    mcp = parts / "mcps" / bad
    shutil.copytree(parts / "weather", mcp)
    with pytest.raises(StackInvalid, match=r"invalid MCP component name .* not a Windows device name"):
        build(tmp_path / "m", compose("c", "1", base=parts / "base", mcp=[mcp]))


@pytest.mark.parametrize("good", ["console", "com10", "aux-tools"])
def test_similar_but_safe_names_are_accepted(tmp_path, parts, good):
    skill = parts / "ok-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(f"---\nname: {good}\n---\nbody\n", encoding="utf-8")
    config, _ = build(tmp_path, compose("c", "1", base=parts / "base", skills=[skill]))
    assert (config / "skills" / good / "SKILL.md").is_file()


def test_mcp_file_named_like_a_device_falls_back_to_its_folder(tmp_path, parts):
    folder = parts / "weather-pack"
    folder.mkdir()
    (folder / "nul.json").write_text(json.dumps({"mcpServers": {"w": {"command": "python"}}}), encoding="utf-8")
    config, provenance = build(tmp_path, compose("c", "1", base=parts / "base", mcp=[folder / "nul.json"]))
    assert [p.name for p in provenance if p.kind == "mcp"] == ["weather-pack"]
    # a parent folder that is itself unsafe falls back to "mcp"
    bad = parts / "con"
    bad.mkdir()
    (bad / ".mcp.json").write_text(json.dumps({"mcpServers": {"w2": {"command": "python"}}}), encoding="utf-8")
    _, provenance = build(tmp_path / "b", compose("c", "1", base=parts / "base", mcp=[bad / ".mcp.json"]))
    assert [p.name for p in provenance if p.kind == "mcp"] == ["mcp"]


@pytest.mark.parametrize("bad", ["con.md", "nul.md", "aux.md"])
def test_agent_file_names_follow_the_rule(tmp_path, parts, bad):
    agent = parts / bad
    agent.write_text("# Agent\n", encoding="utf-8")
    with pytest.raises(StackInvalid, match="invalid agent file name"):
        build(tmp_path, compose("c", "1", base=parts / "base", agent=agent))


def test_hook_bundle_name_conflicts_with_file(tmp_path, parts):
    """Hook bundle named after an existing file raises StackInvalid."""
    custom_base = parts / "custombase2"
    shutil.copytree(parts / "base", custom_base)

    guard_py_bundle = parts / "guard.py"
    guard_py_bundle.mkdir()
    (guard_py_bundle / "hooks.json").write_text(json.dumps({"hooks": [
        {"event": "post_tool", "command": "echo test"}]}), encoding="utf-8")

    with pytest.raises(StackInvalid, match="hook bundle 'guard.py' conflicts with a hooks file"):
        build(tmp_path, compose("c", "1", base=custom_base,
                               hooks=[{"source": str(guard_py_bundle), "replace": True}]))




def test_hook_bundle_named_hooks_json(tmp_path, parts):
    """Hook bundle named hooks.json raises StackInvalid."""
    hooks_json_dir = parts / "hooks.json"
    hooks_json_dir.mkdir(exist_ok=True)
    (hooks_json_dir / "hooks.json").write_text(json.dumps({"hooks": [
        {"event": "post_tool", "command": "echo test"}]}), encoding="utf-8")

    with pytest.raises(StackInvalid, match="hook bundle 'hooks.json' conflicts with a hooks file"):
        build(tmp_path, compose("c", "1", base=parts / "base",
                               hooks=[{"source": str(hooks_json_dir), "replace": True}]))


def test_frontmatter_name_only_top_level(tmp_path, parts):
    """Only accept name: key at top level, not in nested objects."""
    nested = parts / "nested"
    nested.mkdir()
    (nested / "SKILL.md").write_text(
        "---\nmetadata:\n  name: nested\nname: real\n---\nbody\n",
        encoding="utf-8"
    )

    config, _ = build(tmp_path, compose("c", "1", base=parts / "base", skills=[nested]))
    stack = Stack.load(config)
    assert any(s.name == "real" for s in stack.skills)


def _mcp_folder(root, name, server, script="s.py"):
    d = root / name
    d.mkdir(parents=True)
    (d / ".mcp.json").write_text(json.dumps({"mcpServers": {server: {
        "command": "python", "args": [f"${{here}}/{script}"]}}}), encoding="utf-8")
    (d / script).write_text("print(1)\n", encoding="utf-8")
    return d


def test_mcp_folder_replace_is_segment_aware(tmp_path, parts):
    github = _mcp_folder(parts / "a", "github", "gh")
    git1 = _mcp_folder(parts / "b", "git", "git1")
    git2 = _mcp_folder(parts / "c", "git", "git2")
    config, _ = build(tmp_path, compose("c", "1", base=parts / "base",
                                        mcp=[github, git1, {"source": str(git2), "replace": True}]))
    servers = Stack.load(config).mcp_servers
    assert "gh" in servers and "git2" in servers and "git1" not in servers


def test_hook_bundle_replace_drops_the_base_bundle_entries(tmp_path, parts):
    base = parts / "bundled-base"
    shutil.copytree(parts / "base", base)
    (base / "hooks" / "guard").mkdir()
    (base / "hooks" / "guard" / "g.py").write_text("print(1)\n", encoding="utf-8")
    hooks_file = base / "hooks" / "hooks.json"
    data = json.loads(hooks_file.read_text(encoding="utf-8"))
    data["hooks"].append({"event": "post_tool", "command": "python ${stack}/hooks/guard/g.py"})
    hooks_file.write_text(json.dumps(data), encoding="utf-8")
    new = parts / "new" / "guard"
    new.mkdir(parents=True)
    (new / "hooks.json").write_text(json.dumps({"hooks": [
        {"event": "post_tool", "command": "python ${here}/n.py"}]}), encoding="utf-8")
    (new / "n.py").write_text("print(2)\n", encoding="utf-8")
    config, _ = build(tmp_path, compose("c", "1", base=base, hooks=[{"source": str(new), "replace": True}]))
    commands = [h.command for h in Stack.load(config).hooks]
    assert [c for c in commands if "/hooks/guard/" in c] == ["python ${stack}/hooks/guard/n.py"]
    assert "python ${stack}/hooks/guard.py" in commands  # the base's other entry stays


def test_file_form_mcp_named_after_its_folder(tmp_path, parts):
    src = parts / "weather" / ".mcp.json"
    src.write_text(json.dumps({"mcpServers": {"weather": {"command": "weather-server"}}}), encoding="utf-8")
    config, provenance = build(tmp_path, compose("c", "1", base=parts / "base", mcp=[src]))
    assert Stack.load(config).mcp_servers["weather"]["command"] == "weather-server"
    assert [p.name for p in provenance if p.kind == "mcp"] == ["weather"]
    assert not (config / "mcp" / "weather").exists()


def test_file_form_mcp_refuses_here(tmp_path, parts):
    with pytest.raises(StackInvalid, match=r"\$\{here\} needs a folder source"):
        build(tmp_path, compose("c", "1", base=parts / "base", mcp=[parts / "weather" / ".mcp.json"]))


def test_non_utf8_skill_md_is_stack_invalid(tmp_path, parts):
    bad = parts / "badskill"
    bad.mkdir()
    (bad / "SKILL.md").write_bytes(b"---\nname: \xff\xfe\n---\n")
    with pytest.raises(StackInvalid, match=r"skills\[0\]: SKILL.md"):
        build(tmp_path, compose("c", "1", base=parts / "base", skills=[bad]))


@pytest.mark.parametrize("rel, key, match", [
    (".mcp.json", "mcpServers", "base: 'mcpServers' must be an object"),
    ("hooks/hooks.json", "hooks", "base: 'hooks' must be an array"),
])
def test_base_null_sections_are_stack_invalid(tmp_path, parts, rel, key, match):
    base = parts / "nullbase"
    shutil.copytree(parts / "base", base)
    (base / rel).write_text(json.dumps({key: None}), encoding="utf-8")
    with pytest.raises(StackInvalid, match=match):
        build(tmp_path, compose("c", "1", base=base))


def test_copy_failure_is_stack_fetch_error(tmp_path, parts, monkeypatch):
    def broken(src, dst, **kw):
        raise shutil.Error([(str(src), str(dst), "sig=SECRET permission denied")])
    monkeypatch.setattr(shutil, "copytree", broken)
    with pytest.raises(StackFetchError, match=r"skills\[0\]") as e:
        build(tmp_path, compose("c", "1", skills=[parts / "extra-skill"]))
    assert "SECRET" not in str(e.value)
