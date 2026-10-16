import os
from pathlib import Path

import pytest

from phd.errors import StackInvalid
from phd.stacks.manifest import compose, load_manifest


def test_description_must_be_a_string():
    with pytest.raises(StackInvalid, match="'description' must be a string"):
        compose("n", "1", description=5)


@pytest.mark.parametrize("models", ["claude=haiku", ["claude"], {"claude": 3}])
def test_default_models_must_be_a_table_of_strings(models):
    with pytest.raises(StackInvalid, match="'default_models' must be a table of strings"):
        compose("n", "1", default_models=models)


def test_a_toml_entry_must_be_a_table(tmp_path):
    path = tmp_path / "stack.toml"
    path.write_text('name = "n"\nversion = "1"\nskills = ["./just-a-string"]\n', encoding="utf-8")
    with pytest.raises(StackInvalid, match=r"skills\[0\]: must be a table with a 'source'"):
        load_manifest(path)


def test_a_pathlike_source_is_accepted(tmp_path):
    m = compose("n", "1", base=tmp_path / "b", skills=[{"source": tmp_path / "s"}])
    assert m.base.source == str((tmp_path / "b").resolve())
    assert m.skills[0].source == str((tmp_path / "s").resolve())


@pytest.mark.parametrize("source", ["", "   ", 5])
def test_source_must_be_a_non_empty_string(source):
    with pytest.raises(StackInvalid, match=r"skills\[0\]: source must be a non-empty string"):
        compose("n", "1", skills=[{"source": source}])


def test_url_source_gets_a_canonical_form():
    m = compose("n", "1", base="HTTPS://Host.Example/a/b.zip?x=1#f")
    assert m.base.source == "https://Host.Example/a/b.zip?x=1#f"


def test_unusable_home_is_stack_invalid(monkeypatch):
    def no_home(self):
        raise RuntimeError("Could not determine home directory")
    monkeypatch.setattr(Path, "expanduser", no_home)
    with pytest.raises(StackInvalid, match=r"skills\[0\]: invalid path \(Could not determine home"):
        compose("n", "1", skills=["~someone/skill"], root=None)


@pytest.mark.parametrize("exc", [RuntimeError, ValueError])
def test_unresolvable_path_is_stack_invalid(monkeypatch, exc):
    def broken(self, *a, **kw):
        raise exc("loop")
    m_root = Path.cwd()
    with monkeypatch.context() as mp:
        mp.setattr(Path, "resolve", broken)
        with pytest.raises(StackInvalid, match=r"agent: .*invalid path \(loop\)"):
            from phd.stacks import manifest
            manifest._source("rel/agent.md", "agent: ", m_root)
    assert os.path.isabs(compose("n", "1", base="rel").base.source)
