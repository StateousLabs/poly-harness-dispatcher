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


def _deps():
    from phd.harnesses.opencode import deps
    return deps


def test_truncated_package_lock_is_rewritten(tmp_path):
    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / "package-lock.json").write_text('{"name": "config", "lockf', encoding="utf-8")  # cut off mid-write
    _deps().seed(folder)
    assert_seeded(folder)


def test_unparseable_package_json_is_replaced_with_the_default(tmp_path):
    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / "package.json").write_text("{not json", encoding="utf-8")
    _deps().seed(folder)
    assert json.loads((folder / "package.json").read_text(encoding="utf-8")) == {"dependencies": {PLUGIN: "*"}}
    assert_seeded(folder)


def test_seed_writes_through_unique_temp_files(tmp_path, monkeypatch):
    """The catalogue home is shared by processes: a fixed `<name>.tmp` would let two seeders collide."""
    import os
    deps = _deps()
    seen = []
    real = os.replace
    monkeypatch.setattr(deps.os, "replace", lambda a, b: (seen.append(Path(a).name), real(a, b))[1])
    deps.seed(tmp_path / "cfg")
    assert len(seen) == 2 and all(str(os.getpid()) in n and n.endswith(".tmp") for n in seen), seen
    assert len(set(seen)) == 2 and not any(n in ("package.json.tmp", "package-lock.json.tmp") for n in seen)
