"""Pre-empt OpenCode's per-config-folder dependency install (item 9).

On every start opencode 1.18.18 runs an in-process npm (Arborist) install of `@opencode-ai/plugin` into each config
folder it loads (`OPENCODE_CONFIG_DIR` and `$XDG_CONFIG_HOME/opencode`), and waits for it whenever a plugin is
configured. PHD's hooks are a plugin, so a fresh run home paid 17-100+ s (61 MB per folder), and an install cut off
by a kill left a lock that made the next turn in that home wait 60 s more. opencode skips the install when the folder
has `node_modules/` and its `package-lock.json` lists, under `packages[""]`, every dependency named in `package.json`
plus `@opencode-ai/plugin`; only names are compared. PHD's plugin imports nothing from that package.

`seed` writes exactly that state, once: an existing `package-lock.json` (a resumed home, or a real install by
opencode) is never touched. If a later opencode checks more, it simply installs again: slow, but correct, and
`npm_env` keeps that install in PHD's cache instead of the host's npm cache."""

import contextlib
import json
from pathlib import Path

from ..._util.fs import write_json_atomic
from ..._util.paths import phd_cache_dir

PLUGIN_PACKAGE = "@opencode-ai/plugin"
_DEP_KEYS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


def config_dirs(home: Path) -> list[Path]:
    """The folders opencode installs into for a home laid out as PHD's (runs and the model catalogue)."""
    return [home / "config", home / "xdg-config" / "opencode"]


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def seed(folder: Path) -> None:
    """Make `folder` look installed to opencode unless it already has a `package-lock.json`. Best effort: if it
    cannot be written (a concurrent writer of the shared catalogue home, a full disk), opencode installs as before."""
    lock = folder / "package-lock.json"
    if lock.exists():
        return
    with contextlib.suppress(OSError):
        (folder / "node_modules").mkdir(parents=True, exist_ok=True)
        pkg_path = folder / "package.json"
        if pkg_path.exists():  # e.g. left by an install that was cut off: list what it names, keep the file
            pkg = _read_json(pkg_path)
        else:
            pkg = {"dependencies": {PLUGIN_PACKAGE: "*"}}
            write_json_atomic(pkg_path, pkg)
        wanted = {PLUGIN_PACKAGE: "*"}
        for key in _DEP_KEYS:
            section = pkg.get(key)
            if isinstance(section, dict):
                wanted.update({name: str(spec) for name, spec in section.items()})
        write_json_atomic(lock, {"name": folder.name, "lockfileVersion": 3, "requires": True,
                                 "packages": {"": {"dependencies": wanted}}})


def seed_home(home: Path) -> None:
    for folder in config_dirs(home):
        seed(folder)


def npm_env() -> dict[str, str]:
    """npm settings for every opencode process: a PHD-owned npm cache, and an empty PHD-owned npmrc in place of the
    user's `~/.npmrc` and the global npmrc (opencode's embedded npm reads both, through `@npmcli/config`). Both
    spellings are set, with one value: on Windows the host's variable arrives upper-case and must be overridden."""
    base = phd_cache_dir() / "opencode"
    cache = base / "npm-cache"
    rc = base / "npmrc"
    cache.mkdir(parents=True, exist_ok=True)
    if not rc.exists():
        with contextlib.suppress(FileExistsError):
            rc.open("x", encoding="utf-8").close()
    env = {}
    for name, value in (("cache", cache), ("userconfig", rc), ("globalconfig", rc)):
        env[f"npm_config_{name}"] = env[f"NPM_CONFIG_{name.upper()}"] = str(value)
    return env
