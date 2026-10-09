"""The model catalogue command runs isolated from the host (item 8): no host harness/auth/XDG variables, PHD-owned
homes and cwd, the spec's credentials, and a cache key that includes the credential identity."""
import json
import os
import sys
import time
import warnings
from pathlib import Path

import pytest

from conftest import FAKE_HARNESS
from phd import Dispatcher
from phd.errors import MissingCredentials, ModelNotSupported
from phd.harnesses import catalog
from phd.harnesses.catalog import ModelCatalog
from phd.harnesses.codex import models as codex_models
from phd.harnesses.opencode import models as opencode_models
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
        auth = Path(env["CODEX_HOME"]) / "auth.json" if "CODEX_HOME" in env else None
        calls.append({"argv": argv, "env": env, "cwd": Path(cwd),
                      "auth": auth.read_text(encoding="utf-8") if auth and auth.exists() else None})
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
    assert Path(env["XDG_CACHE_HOME"]).parent == phd_cache_dir / "opencode" / "xdg-cache"
    assert env["XDG_CACHE_HOME"] == str(opencode_models.shared_cache_dir(None))
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert json.loads(env["OPENCODE_AUTH_CONTENT"]) == {"opencode": {"type": "api", "key": "oc-test"}}
    assert cwd == home and cwd.is_dir() and cwd != Path.cwd()


def test_codex_catalogue_env_is_isolated(host_env, captured, credentials, phd_cache_dir):
    ModelCatalog().models("codex", credentials["codex"])
    (call,) = captured
    env, cwd = call["env"], call["cwd"]
    assert_no_host_values(env, host_env)
    codex_home = Path(env["CODEX_HOME"])
    assert codex_home.parent.parent == phd_cache_dir / "codex" / "catalog-home"  # catalog-home/<identity>/<uuid>
    assert call["auth"] == credentials["codex"].path.read_text(encoding="utf-8")  # present while the command runs
    assert cwd == codex_home
    assert not any(k.startswith(("OPENCODE_", "XDG_")) for k in env)


def test_codex_catalogue_leaves_no_credential_copy_at_rest(captured, credentials, phd_cache_dir):
    ModelCatalog().models("codex", credentials["codex"])
    assert not Path(captured[0]["env"]["CODEX_HOME"]).exists()
    assert list(phd_cache_dir.rglob("auth.json")) == []


def test_codex_catalogue_home_is_removed_when_the_command_fails(monkeypatch, credentials, phd_cache_dir):
    homes = []

    def failing(argv, *, env, cwd):
        homes.append(Path(env["CODEX_HOME"]))
        raise RuntimeError("codex exited 1")
    monkeypatch.setattr(catalog, "_run_cli", failing)
    assert "was not checked" in ModelCatalog().validate("codex", "gpt-5.6-terra", credentials["codex"])[0]
    assert not homes[0].exists() and list(phd_cache_dir.rglob("auth.json")) == []


@pytest.mark.parametrize("harness", ["codex", "opencode"])
def test_unusable_cache_dir_is_a_warning(harness, monkeypatch, tmp_path, captured, credentials):
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("PHD_CACHE_DIR", str(blocker / "cache"))
    model = {"codex": "gpt-5.6-terra", "opencode": "opencode/big-pickle"}[harness]
    warns = ModelCatalog().validate(harness, model, credentials[harness])
    assert len(warns) == 1 and "was not checked" in warns[0]
    assert captured == []


def test_unreadable_codex_auth_raises_missing_credentials(captured, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    creds = Credentials.codex_auth_file(auth)
    auth.unlink()
    with pytest.raises(MissingCredentials):
        ModelCatalog().validate("codex", "gpt-5.6-terra", creds)


def test_opencode_catalogue_miss_is_a_warning_codex_still_raises(captured, credentials):
    warns = ModelCatalog().validate("opencode", "opencode/brand-new-free", credentials["opencode"])
    assert len(warns) == 1 and "not in the opencode model catalogue" in warns[0] and "model_unavailable" in warns[0]
    with pytest.raises(ModelNotSupported):
        ModelCatalog().validate("codex", "gpt-5.6-tera", credentials["codex"])


def test_shared_opencode_cache_is_keyed_by_executable(tmp_path, make_spec, inputs):
    a, b = tmp_path / "oc-a.exe", tmp_path / "oc-b.exe"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")
    assert opencode_models.shared_cache_dir([str(a)]) != opencode_models.shared_cache_dir([str(b)])
    assert opencode_models.shared_cache_dir([str(a)]) == opencode_models.shared_cache_dir([str(a)])


def test_catalogue_and_runs_share_the_cache_of_the_same_executable(captured, tmp_path, make_spec, inputs):
    exe = [sys.executable, str(tmp_path / "wrapper.py")]
    d = Dispatcher(make_spec("opencode"), runs_root=tmp_path / "runs", executables={"opencode": exe})
    m = d.plan(inputs)
    assert m.env["XDG_CACHE_HOME"] == captured[0]["env"]["XDG_CACHE_HOME"] == \
        str(opencode_models.shared_cache_dir(exe))
    assert m.env["XDG_CACHE_HOME"] != str(opencode_models.shared_cache_dir(None))


def test_seed_prunes_stale_refresh_temp_files(captured, credentials):
    cache = opencode_models.shared_cache_dir(None) / "opencode"
    cache.mkdir(parents=True)
    old, fresh = cache / "models.json.111.222.tmp", cache / "models.json.333.444.tmp"
    for f in (old, fresh):
        f.write_text("partial", encoding="utf-8")
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    ModelCatalog().models("opencode", credentials["opencode"])
    assert not old.exists() and fresh.exists()


def test_temp_cache_dir_warns_once_for_codex(monkeypatch, captured, credentials):
    monkeypatch.setattr(codex_models, "_warned_temp", False)
    with pytest.warns(RuntimeWarning, match="temp"):
        ModelCatalog().models("codex", credentials["codex"])  # tests' PHD_CACHE_DIR is under tmp_path
    catalog._PROCESS_CACHE.clear()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ModelCatalog().models("codex", credentials["codex"])


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
    seeded = opencode_models.shared_cache_dir(None) / "opencode" / "models.json"
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


def test_codex_load_sweeps_leftover_homes_older_than_ten_minutes(captured, credentials, phd_cache_dir):
    root = phd_cache_dir / "codex" / "catalog-home"
    old, fresh = root / "someid" / "deadbeef", root / "someid" / "cafef00d"
    for d in (old, fresh):
        d.mkdir(parents=True)
        (d / "auth.json").write_text("{}", encoding="utf-8")
    long_ago = time.time() - 3600
    os.utime(old, (long_ago, long_ago))
    ModelCatalog().models("codex", credentials["codex"])
    assert not old.exists() and fresh.is_dir()


def test_codex_home_removal_failure_is_a_warning_and_keeps_the_list(monkeypatch, captured, credentials):
    def broken_rmtree(path):
        raise PermissionError(13, "in use", str(path))
    monkeypatch.setattr(codex_models, "robust_rmtree", broken_rmtree)
    cat = ModelCatalog()
    warns = cat.validate("codex", "gpt-5.6-terra", credentials["codex"])
    assert len(warns) == 1 and "could not remove" in warns[0] and "auth.json" in warns[0]
    assert cat.models("codex", credentials["codex"]) == ["gpt-5.6-terra"]


def test_catalogue_timeout_kills_the_whole_process_tree(monkeypatch, tmp_path, credentials, phd_cache_dir):
    from helpers import wait_dead
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CHILD_PID", str(pid_file))
    monkeypatch.setattr(catalog, "CATALOG_TIMEOUT_S", 3)
    hanging = [sys.executable, str(Path(FAKE_HARNESS).parent / "hanging_catalog.py")]
    warns = ModelCatalog(executables={"codex": hanging}).validate("codex", "gpt-5.6-terra", credentials["codex"])
    assert len(warns) == 1 and "was not checked" in warns[0]
    assert wait_dead(int(pid_file.read_text(encoding="utf-8")), timeout=10), "the catalogue's child survived"
    assert list(phd_cache_dir.rglob("auth.json")) == []


def test_install_key_ignores_the_comspec_wrapper(monkeypatch, tmp_path):
    shim = tmp_path / "opencode.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    cmd_a, cmd_b = tmp_path / "a" / "cmd.exe", tmp_path / "b" / "cmd.exe"
    for exe, body in ((cmd_a, "old build"), (cmd_b, "patched build, other size")):
        exe.parent.mkdir()
        exe.write_text(body, encoding="utf-8")
    keys = []
    for comspec in (cmd_a, cmd_b):
        monkeypatch.setattr(opencode_models, "resolve_argv", lambda argv, c=comspec: [str(c), "/c", str(shim)])
        keys.append(opencode_models.install_key(None))
    assert keys[0] == keys[1]
    monkeypatch.setattr(opencode_models, "resolve_argv", lambda argv: [str(cmd_a), "/c", str(tmp_path / "other.cmd")])
    assert opencode_models.install_key(None) != keys[0]
