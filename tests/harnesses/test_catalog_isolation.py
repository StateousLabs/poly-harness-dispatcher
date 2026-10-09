"""The model catalogue command runs isolated from the host (item 8): no host harness/auth/XDG variables, PHD-owned
homes and cwd, the spec's credentials, and a cache key that includes the credential identity."""
import json
import os
import sys
from pathlib import Path

import pytest

from conftest import FAKE_HARNESS
from phd import Dispatcher
from phd.harnesses import catalog
from phd.harnesses.catalog import ModelCatalog
from phd.spec import Credentials

SLOW = [sys.executable, str(Path(FAKE_HARNESS).parent / "slow_catalog.py")]
HOST = {"OPENCODE_CONFIG": "HOST-oc-config", "OPENCODE_CONFIG_DIR": "HOST-oc-config-dir",
        "XDG_DATA_HOME": "HOST-xdg-data", "XDG_CONFIG_HOME": "HOST-xdg-config", "XDG_STATE_HOME": "HOST-xdg-state",
        "CODEX_HOME": "HOST-codex-home", "ANTHROPIC_API_KEY": "HOST-key", "OPENAI_API_KEY": "HOST-key2"}


@pytest.fixture
def host_env(monkeypatch, tmp_path):
    """Host harness variables that must never reach the catalogue command; returns the fake host XDG cache."""
    for k, v in HOST.items():
        monkeypatch.setenv(k, v)
    host_cache = tmp_path / "host-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(host_cache))
    return host_cache


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake_run_cli(argv, *, env, cwd):
        calls.append({"argv": argv, "env": env, "cwd": Path(cwd)})
        if argv[1:3] == ["debug", "models"]:
            return json.dumps({"models": [{"slug": "gpt-5.6-terra"}]})
        return "opencode/big-pickle\n"
    monkeypatch.setattr(catalog, "_run_cli", fake_run_cli)
    return calls


def assert_no_host_values(env, host_cache):
    for k, v in env.items():
        assert not v.startswith("HOST-"), (k, v)
        assert str(host_cache) not in v, (k, v)
    for k in ("HOME", "USERPROFILE"):  # deliberately not redirected
        assert env.get(k) == os.environ.get(k)


def test_opencode_catalogue_env_is_isolated(host_env, captured, credentials, phd_cache_dir):
    assert ModelCatalog().models("opencode", credentials["opencode"]) == ["opencode/big-pickle"]
    (call,) = captured
    env, cwd = call["env"], call["cwd"]
    assert_no_host_values(env, host_env)
    home = phd_cache_dir / "opencode" / "catalog-home"
    assert env["XDG_DATA_HOME"] == str(home / "data")
    assert env["XDG_CONFIG_HOME"] == str(home / "xdg-config")
    assert env["XDG_STATE_HOME"] == str(home / "state")
    assert env["OPENCODE_CONFIG_DIR"] == str(home / "config")
    assert env["XDG_CACHE_HOME"] == str(phd_cache_dir / "opencode" / "xdg-cache")
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert json.loads(env["OPENCODE_AUTH_CONTENT"]) == {"opencode": {"type": "api", "key": "oc-test"}}
    assert cwd == home and cwd.is_dir() and cwd != Path.cwd()


def test_codex_catalogue_env_is_isolated(host_env, captured, credentials, phd_cache_dir):
    ModelCatalog().models("codex", credentials["codex"])
    (call,) = captured
    env, cwd = call["env"], call["cwd"]
    assert_no_host_values(env, host_env)
    codex_home = Path(env["CODEX_HOME"])
    assert codex_home.parent == phd_cache_dir / "codex" / "catalog-home"
    assert (codex_home / "auth.json").read_text(encoding="utf-8") == \
        credentials["codex"].path.read_text(encoding="utf-8")
    assert cwd == codex_home
    assert not any(k.startswith(("OPENCODE_", "XDG_")) for k in env)


def test_credential_identity_is_part_of_the_cache_key(captured, tmp_path):
    def oc(name, key):
        p = tmp_path / name
        p.write_text(json.dumps({"opencode": {"type": "api", "key": key}}), encoding="utf-8")
        return Credentials.opencode_auth_file(p)
    a, b = oc("a.json", "secret-AAA"), oc("b.json", "secret-BBB")
    ModelCatalog().models("opencode", a)
    ModelCatalog().models("opencode", a)  # same credential, new instance: process cache hit
    ModelCatalog().models("opencode", b)
    shared = ModelCatalog()
    shared.models("opencode", a)
    shared.models("opencode", b)  # one instance, two credentials: still two identities
    assert len(captured) == 2
    keys = repr(list(catalog._PROCESS_CACHE))
    assert "secret-AAA" not in keys and "secret-BBB" not in keys and "a.json" not in keys


def test_token_credentials_never_appear_in_the_cache_key(captured):
    from phd.harnesses.catalog import credential_identity
    ident = credential_identity(Credentials.anthropic_api_key("sk-very-secret"))
    assert "sk-very-secret" not in ident
    assert ident != credential_identity(Credentials.anthropic_api_key("sk-other"))
    assert credential_identity(None) == "none"


def test_catalogue_seeds_the_phd_cache_from_the_host_without_writing_it(host_env, captured, credentials,
                                                                        phd_cache_dir):
    host_models = host_env / "opencode" / "models.json"
    host_models.parent.mkdir(parents=True)
    host_models.write_text('{"opencode": {}}', encoding="utf-8")
    before = host_models.stat().st_mtime_ns
    ModelCatalog().models("opencode", credentials["opencode"])
    seeded = phd_cache_dir / "opencode" / "xdg-cache" / "opencode" / "models.json"
    assert seeded.read_text(encoding="utf-8") == '{"opencode": {}}'
    assert host_models.stat().st_mtime_ns == before
    seeded.write_text('{"newer": {}}', encoding="utf-8")  # an existing PHD copy is never overwritten
    catalog._PROCESS_CACHE.clear()
    ModelCatalog().models("opencode", credentials["opencode"])
    assert seeded.read_text(encoding="utf-8") == '{"newer": {}}'


def test_injected_runner_still_takes_only_argv(credentials):
    seen = []
    cat = ModelCatalog(lambda argv: seen.append(argv) or "opencode/big-pickle\n")
    assert cat.validate("opencode", "opencode/big-pickle", credentials["opencode"]) == []
    assert seen == [["opencode", "models"]]


def test_dispatcher_runs_a_real_catalogue_process_isolated(host_env, tmp_path, monkeypatch, make_spec, inputs,
                                                           phd_cache_dir):
    out = tmp_path / "catalog-env.json"
    monkeypatch.setenv("FAKE_CATALOG_ENV_OUT", str(out))
    monkeypatch.setenv("FAKE_CATALOG_MODELS", "opencode/big-pickle")
    d = Dispatcher(make_spec("opencode"), runs_root=tmp_path / "runs", executables={"opencode": SLOW})
    d.plan(inputs)
    seen = json.loads(out.read_text(encoding="utf-8"))
    assert_no_host_values(seen["env"], host_env)
    assert Path(seen["cwd"]) == phd_cache_dir / "opencode" / "catalog-home"
    assert json.loads(seen["env"]["OPENCODE_AUTH_CONTENT"]) == {"opencode": {"type": "api", "key": "oc-test"}}
