"""Pre-empt OpenCode's per-config-folder dependency install (item 9).

On every start opencode 1.18.18 runs an in-process npm (Arborist) install of `@opencode-ai/plugin` into each config
folder it loads (`OPENCODE_CONFIG_DIR` and `$XDG_CONFIG_HOME/opencode`), and waits for it whenever a plugin is
configured. PHD's hooks are a plugin, so a fresh run home paid 17-100+ s (61 MB per folder), and an install cut off
by a kill left a lock that made the next turn in that home wait 60 s more. opencode skips the install when the folder
has `node_modules/` and its `package-lock.json` lists, under `packages[""]`, every dependency named in `package.json`
plus `@opencode-ai/plugin`; only names are compared. PHD's plugin imports nothing from that package.

`seed` writes exactly that state, once: an existing `package-lock.json` (a resumed home, or a real install by
opencode) is never touched. If a later opencode checks more, it simply installs again: slow, but correct, and
`_util.npm.npm_env` (every run's env) keeps that install in PHD's npm cache instead of the host's."""

import contextlib
import json
import os
import uuid
from pathlib import Path

from ..._util.fs import write_json

PLUGIN_PACKAGE = "@opencode-ai/plugin"
_DEP_KEYS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


def config_dirs(home: Path) -> list[Path]:
    """The folders opencode installs into for a home laid out as PHD's (runs and the model catalogue)."""
    return [home / "config", home / "xdg-config" / "opencode"]


def _read_json(path: Path) -> dict | None:
    """The file's JSON object, or None if it is missing, unreadable or not a JSON object (for example truncated)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_atomic(path: Path, data: dict) -> None:
    """Through a temp file unique to this process and call: the catalogue home is shared by PHD processes."""
    tmp = path.with_name(f"{path.name}.phd-{os.getpid()}-{uuid.uuid4().hex}.tmp")
    try:
        write_json(tmp, data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def seed(folder: Path) -> None:
    """Make `folder` look installed to opencode unless it already has a valid `package-lock.json` (a truncated one is
    rewritten). Best effort: if it cannot be written (a full disk, a locked file), opencode installs as before."""
    lock = folder / "package-lock.json"
    if _read_json(lock) is not None:
        return
    with contextlib.suppress(OSError):
        (folder / "node_modules").mkdir(parents=True, exist_ok=True)
        pkg_path = folder / "package.json"
        pkg = _read_json(pkg_path)  # e.g. left by an install that was cut off: list what it names, keep the file
        if pkg is None:  # missing or unparseable: the default
            pkg = {"dependencies": {PLUGIN_PACKAGE: "*"}}
            _write_atomic(pkg_path, pkg)
        wanted = {PLUGIN_PACKAGE: "*"}
        for key in _DEP_KEYS:
            section = pkg.get(key)
            if isinstance(section, dict):
                wanted.update({name: str(spec) for name, spec in section.items()})
        _write_atomic(lock, {"name": folder.name, "lockfileVersion": 3, "requires": True,
                             "packages": {"": {"dependencies": wanted}}})


def seed_home(home: Path) -> None:
    for folder in config_dirs(home):
        seed(folder)
