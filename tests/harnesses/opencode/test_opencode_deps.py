"""OpenCode's per-config-folder dependency install is pre-empted by a seed (item 9).

opencode 1.18.18 runs an in-process npm install of `@opencode-ai/plugin` into every config folder on every start and,
when a plugin is configured (PHD's hooks are), waits for it: 17-100+ s per fresh home. It skips the install when the
folder has `node_modules/` and a `package-lock.json` whose `packages[""]` names every wanted dependency."""
import json
from pathlib import Path

from helpers import materialize_spec

PLUGIN = "@opencode-ai/plugin"


def config_dirs(home: Path) -> list[Path]:
    return [home / "config", home / "xdg-config" / "opencode"]


def assert_seeded(folder: Path) -> None:
    assert (folder / "node_modules").is_dir(), folder
    pkg = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((folder / "package-lock.json").read_text(encoding="utf-8"))
    assert PLUGIN in pkg["dependencies"], folder
    assert PLUGIN in lock["packages"][""]["dependencies"], folder
    assert set(pkg["dependencies"]) <= set(lock["packages"][""]["dependencies"]), folder


def test_materialize_seeds_both_config_folders(tmp_path, make_spec, inputs):
    m = materialize_spec(make_spec("opencode"), inputs, run_id="r1", run_dir=tmp_path / "r1")
    for folder in config_dirs(m.home):
        assert_seeded(folder)


def test_existing_package_lock_is_never_overwritten(tmp_path, make_spec, inputs):
    spec = make_spec("opencode")
    m = materialize_spec(spec, inputs, run_id="r1", run_dir=tmp_path / "r1")
    real = '{"name": "config", "lockfileVersion": 3, "packages": {"": {"dependencies": {"x": "1"}}}}'
    for folder in config_dirs(m.home):
        (folder / "package-lock.json").write_text(real, encoding="utf-8")
        (folder / "package.json").write_text('{"dependencies": {"x": "1"}}', encoding="utf-8")
    # a resumed turn re-materializes the same home
    materialize_spec(spec, inputs, run_id="r2", run_dir=tmp_path / "r2", home=m.home)
    for folder in config_dirs(m.home):
        assert (folder / "package-lock.json").read_text(encoding="utf-8") == real
        assert (folder / "package.json").read_text(encoding="utf-8") == '{"dependencies": {"x": "1"}}'


def test_seed_keeps_an_existing_package_json_and_lists_its_deps(tmp_path):
    from phd.harnesses.opencode import deps
    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / "package.json").write_text('{"dependencies": {"left-pad": "1.3.0"}}', encoding="utf-8")
    deps.seed(folder)
    assert json.loads((folder / "package.json").read_text(encoding="utf-8")) == {"dependencies": {"left-pad": "1.3.0"}}
    lock = json.loads((folder / "package-lock.json").read_text(encoding="utf-8"))
    assert set(lock["packages"][""]["dependencies"]) == {"left-pad", PLUGIN}


def test_seed_is_not_a_phd_owned_path(tmp_path, make_spec, inputs):
    """Owned paths are deleted on resume; the seed (later possibly opencode's real install) must survive."""
    from phd.harnesses import get_adapter
    m = materialize_spec(make_spec("opencode"), inputs, run_id="r1", run_dir=tmp_path / "r1")
    owned = {p.resolve() for p in get_adapter("opencode").owned_paths(m.home)}
    for folder in config_dirs(m.home):
        for name in ("node_modules", "package.json", "package-lock.json"):
            assert (folder / name).resolve() not in owned


def test_run_env_points_npm_at_phd_owned_cache_and_empty_userconfig(tmp_path, make_spec, inputs, phd_cache_dir):
    m = materialize_spec(make_spec("opencode"), inputs, run_id="r1", run_dir=tmp_path / "r1")
    cache = Path(m.env["npm_config_cache"])
    assert cache.is_relative_to(phd_cache_dir) and m.env["NPM_CONFIG_CACHE"] == str(cache)
    for key in ("npm_config_userconfig", "npm_config_globalconfig"):
        rc = Path(m.env[key])
        assert rc.is_relative_to(phd_cache_dir) and rc.is_file() and rc.read_text(encoding="utf-8") == "", key
        assert m.env[key.upper()] == str(rc)
