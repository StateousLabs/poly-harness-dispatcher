"""Every harness's agent uses PHD's npm cache and an empty PHD-owned npmrc, never the host's (item 9 fix round).

Same as a future sandbox run, where no host npm config exists: no harness writes the host npm cache or reads the
host `~/.npmrc` / global npmrc."""
from pathlib import Path

import pytest
from helpers import materialize_spec

NAMES = ("cache", "userconfig", "globalconfig")


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode"])
def test_run_env_redirects_npm_to_phd_owned_paths(harness, tmp_path, make_spec, inputs, phd_cache_dir):
    m = materialize_spec(make_spec(harness), inputs, run_id="r1", run_dir=tmp_path / "r1")
    values = {}
    for name in NAMES:
        lower, upper = m.env[f"npm_config_{name}"], m.env[f"NPM_CONFIG_{name.upper()}"]
        assert lower == upper, name
        assert Path(lower).is_relative_to(phd_cache_dir), (name, lower)
        values[name] = Path(lower)
    assert values["cache"].is_dir()
    assert values["userconfig"] == values["globalconfig"]


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_npmrc_is_an_empty_phd_owned_file(harness, tmp_path, make_spec, inputs, phd_cache_dir):
    m = materialize_spec(make_spec(harness), inputs, run_id="r1", run_dir=tmp_path / "r1")
    rc = Path(m.env["npm_config_userconfig"])
    assert rc.is_file() and rc.read_text(encoding="utf-8") == ""
    assert not rc.is_relative_to(m.home)  # shared, outside every run home
