import json
import tomllib

import pytest
from helpers import materialize_spec

from phd.harnesses import get_adapter
from phd.harnesses.codex import adapter as codex_adapter


@pytest.fixture
def fake_user_home(tmp_path, monkeypatch):
    home = tmp_path / "userhome"
    leaky = home / ".agents" / "skills" / "leaky"
    leaky.mkdir(parents=True)
    (leaky / "SKILL.md").write_text("---\nname: leaky\ndescription: x\n---\n", encoding="utf-8")
    monkeypatch.setattr(codex_adapter, "user_home", lambda: home)
    return home


def test_codex_home_layout_and_argv(tmp_path, make_spec, inputs, workdir, fake_user_home):
    m = materialize_spec(make_spec("codex"), inputs, run_id="r1", run_dir=tmp_path / "r1")
    home = m.home
    assert (home / "skills" / "demo-notes" / "SKILL.md").is_file()
    agents = (home / "AGENTS.md").read_text(encoding="utf-8")
    assert "HELPER-IDENTITY-OK" in agents and "RULES-OK" in agents and "MANGO" not in agents
    assert json.loads((home / "auth.json").read_text(encoding="utf-8"))["auth_mode"] == "chatgpt"

    cfg = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert cfg["model"] == "gpt-5.6-terra" and cfg["approval_policy"] == "never"
    assert cfg["sandbox_mode"] == "workspace-write" and cfg["project_doc_max_bytes"] == 0
    echo = cfg["mcp_servers"]["echo"]
    assert echo["command"] == "python" and echo["default_tools_approval_mode"] == "approve"
    assert echo["env"] == {"ECHO_PREFIX": "phd-echo"}
    disabled = [entry["path"] for entry in cfg["skills"]["config"]]
    assert (fake_user_home / ".agents" / "skills" / "leaky" / "SKILL.md").as_posix() in disabled
    assert all(entry["enabled"] is False for entry in cfg["skills"]["config"])

    pre = json.loads((home / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]
    assert pre["matcher"] == "Bash" and pre["hooks"][0]["command"].rstrip('"').endswith("codex 0")

    assert m.argv == ["codex", "exec", "--json", "--strict-config", "--skip-git-repo-check",
                      "-C", str(workdir), "-m", "gpt-5.6-terra", "--ignore-rules",
                      "--dangerously-bypass-hook-trust", "-"]
    assert m.env == {"CODEX_HOME": str(home)}


def test_repo_level_skills_disabled_unless_full(tmp_path, make_spec, inputs, workdir, fake_user_home):
    repo_skill = workdir / ".agents" / "skills" / "repo-skill"
    repo_skill.mkdir(parents=True)
    (repo_skill / "SKILL.md").write_text("---\nname: repo-skill\ndescription: x\n---\n", encoding="utf-8")
    target = (repo_skill / "SKILL.md").as_posix()

    ignore = materialize_spec(make_spec("codex"), inputs, run_id="a", run_dir=tmp_path / "a")
    paths = [e["path"] for e in tomllib.loads((ignore.home / "config.toml").read_text(encoding="utf-8"))["skills"]["config"]]
    assert target in paths

    full = materialize_spec(make_spec("codex", project_config="full"), inputs, run_id="b", run_dir=tmp_path / "b")
    cfg = tomllib.loads((full.home / "config.toml").read_text(encoding="utf-8"))
    assert target not in [e["path"] for e in cfg.get("skills", {}).get("config", [])]
    assert "project_doc_max_bytes" not in cfg and "--ignore-rules" not in full.argv


def test_codex_full_autonomy_and_output_schema(tmp_path, make_spec, inputs, fake_user_home):
    m = materialize_spec(make_spec("codex", autonomy="full", unsafe=True, output_schema={"type": "object"}),
                    inputs, run_id="r", run_dir=tmp_path / "r")
    assert tomllib.loads((m.home / "config.toml").read_text(encoding="utf-8"))["sandbox_mode"] == "danger-full-access"
    schema_path = m.argv[m.argv.index("--output-schema") + 1]
    assert json.loads(open(schema_path, encoding="utf-8").read()) == {"type": "object"}
    assert m.argv[-1] == "-"


def test_codex_resume_argv(make_spec):
    base = ["codex", "exec", "--json", "--strict-config", "--skip-git-repo-check", "-C", "/w", "-m", "gpt-5.6-terra",
            "--ignore-rules", "--dangerously-bypass-hook-trust", "--output-schema", "/h/s.json", "-"]
    assert get_adapter("codex").resume_argv(base, "sid-9", make_spec("codex")) == [
        "codex", "exec", "resume", "sid-9", "--json", "--strict-config", "--skip-git-repo-check",
        "--ignore-rules", "--dangerously-bypass-hook-trust", "--output-schema", "/h/s.json",
        "-m", "gpt-5.6-terra", "-"]


def test_codex_budget_is_a_warning(make_spec):
    warnings = get_adapter("codex").validate(make_spec("codex", budget_usd=1.0))
    assert any("budget_usd" in w for w in warnings)


def _config(tmp_path, make_spec, inputs, fake_user_home, **kw):
    m = materialize_spec(make_spec("codex", **kw), inputs, run_id="r", run_dir=tmp_path / "r")
    return tomllib.loads((m.home / "config.toml").read_text(encoding="utf-8"))


def test_codex_bundled_skills_disabled_by_default(tmp_path, make_spec, inputs, fake_user_home):
    cfg = _config(tmp_path, make_spec, inputs, fake_user_home)
    assert cfg["skills"]["bundled"] == {"enabled": False}
    assert cfg["skills"]["config"]  # foreign-skill disabling still coexists


def test_codex_bundled_skills_opt_in_writes_nothing(tmp_path, make_spec, inputs, fake_user_home):
    cfg = _config(tmp_path, make_spec, inputs, fake_user_home, harness_builtins=True)
    assert "bundled" not in cfg.get("skills", {})
