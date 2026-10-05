import os
from pathlib import Path

import pytest

from phd.errors import StackInvalid
from phd.stacks.manifest import ComponentSource, compose, load_manifest

TOML = '''
name = "reviewer"
version = "1.3.0"
description = "Reviews PRs"
harnesses = ["claude", "codex"]
default_models = { claude = "sonnet" }
base = "https://stacks.example.com/reviewer-1.2.0.tar.gz"
agent = "./agents/AGENTS.md"
base_hooks = false

[[skills]]
source = "./skills/local-one"
[[skills]]
source = "https://x.example.com/pdf.zip"
replace = true
headers = { Authorization = "Bearer ${env:STACKS_TOKEN}" }

[[mcp]]
source = "./mcp/github"

[[hooks]]
source = "~/hooks/audit"
'''


def write(tmp_path, text, name="stack.toml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_load_full_manifest(tmp_path):
    m = load_manifest(write(tmp_path, TOML))
    assert m.ref == "reviewer@1.3.0" and m.description == "Reviews PRs"
    assert m.harnesses == ("claude", "codex") and m.default_models == {"claude": "sonnet"}
    assert m.base == ComponentSource("https://stacks.example.com/reviewer-1.2.0.tar.gz")
    assert m.agent.source == str((tmp_path / "agents" / "AGENTS.md").resolve())
    assert m.base_hooks is False
    assert m.skills[0].source == str((tmp_path / "skills" / "local-one").resolve())
    assert m.skills[1] == ComponentSource("https://x.example.com/pdf.zip", replace=True,
                                          headers={"Authorization": "Bearer ${env:STACKS_TOKEN}"})
    assert m.hooks[0].source == str((Path.home() / "hooks" / "audit").resolve())
    assert m.path == (tmp_path / "stack.toml").resolve()
    assert m.skills[1].is_url and not m.skills[0].is_url


def test_compose_equals_parsed(tmp_path):
    parsed = load_manifest(write(tmp_path, TOML))
    built = compose("reviewer", "1.3.0", description="Reviews PRs", harnesses=["claude", "codex"],
                    default_models={"claude": "sonnet"},
                    base="https://stacks.example.com/reviewer-1.2.0.tar.gz", agent="./agents/AGENTS.md",
                    base_hooks=False,
                    skills=["./skills/local-one",
                            {"source": "https://x.example.com/pdf.zip", "replace": True,
                             "headers": {"Authorization": "Bearer ${env:STACKS_TOKEN}"}}],
                    mcp=["./mcp/github"], hooks=["~/hooks/audit"], root=tmp_path)
    assert built == parsed.__class__(**{**parsed.__dict__, "path": None})


@pytest.mark.parametrize("text, match", [
    ('version = "1"', "'name'"),
    ('name = "a"', "'version'"),
    ('name = "a"\nversion = "1"\ncolour = "red"', "unknown key"),
    ('name = "a b"\nversion = "1"', "'name'"),
    ('name = "a"\nversion = "1"\nharnesses = ["vim"]', "harnesses"),
    ('name = "a"\nversion = "1"\nbase_hooks = "no"', "base_hooks"),
    ('name = "a"\nversion = "1"\nbase = "s3://bucket/x.tar.gz"', "only https://"),
    ('name = "a"\nversion = "1"\nbase = "http://example.com/x.tar.gz"', "only https://"),
    ('name = "a"\nversion = "1"\n[[skills]]\nreplace = true', "'source'"),
    ('name = "a"\nversion = "1"\n[[skills]]\nsource = "x"\nreplace = "yes"', "'replace'"),
    ('name = "a"\nversion = "1"\n[[skills]]\nsource = "x"\nwhatever = 1', "unknown key"),
    ('name = "a"\nversion = "1"\nskills = "x"', "'skills'"),
    ('name = "a"\nversion = "1"\n[[skills]]\nsource = "x"\nheaders = { A = 1 }', "'headers'"),
    ('name = [', "invalid TOML"),
])
def test_validation_errors(tmp_path, text, match):
    with pytest.raises(StackInvalid, match=match):
        load_manifest(write(tmp_path, text))


def test_local_http_is_allowed(tmp_path):
    m = load_manifest(write(tmp_path, 'name = "a"\nversion = "1"\nbase = "http://127.0.0.1:8000/x.tar.gz"'))
    assert m.base.is_url


def test_windows_paths_are_paths_not_urls(tmp_path):  # Review Focus 2
    drive = Path(tmp_path).resolve()
    m = compose("a", "1", base=str(drive), skills=[str(drive / "sub dir")])
    assert not m.base.is_url and m.base.source == str(drive)
    assert m.skills[0].source == str(drive / "sub dir")


def test_missing_manifest_file(tmp_path):
    with pytest.raises(StackInvalid, match="cannot read"):
        load_manifest(tmp_path / "nope.toml")
