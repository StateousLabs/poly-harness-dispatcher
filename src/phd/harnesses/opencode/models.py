"""OpenCode model catalogue: `opencode models`, run isolated from the host.

`opencode models` bootstraps a full instance: it opens `$XDG_DATA_HOME/opencode/opencode.db` and registers its cwd
as a project, loads user and project config (and their plugins) and refreshes `$XDG_CACHE_HOME/opencode/models.json`.
So the catalogue gets its own PHD-owned home and cwd, the spec's credentials, and the shared PHD XDG cache that runs
of the same opencode executable also use (`shared_cache_dir`)."""

import contextlib
import hashlib
import os
import shutil
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from ..._util.npm import npm_env
from ..._util.paths import phd_cache_dir
from ..._util.shell import resolve_argv
from ...errors import HarnessNotInstalled
from ...spec import Credentials
from . import deps

STALE_TMP_S = 3600  # opencode's models.json.<pid>.<ts>.tmp left by an interrupted refresh


def load(run: Callable[[list[str]], str]) -> list[str]:
    lines = (line.strip() for line in run(["opencode", "models"]).splitlines())
    return [line for line in lines if "/" in line and " " not in line]


def install_key(executable: Sequence[str] | None) -> str:
    """A short hash of the opencode executable (`executables["opencode"]` or the CLI on PATH): each resolved part's
    real path, size and mtime. Different installs (and an upgraded one) get different caches."""
    argv = list(executable) if executable else ["opencode"]
    with contextlib.suppress(HarnessNotInstalled):
        argv = resolve_argv(argv)
    if len(argv) > 2 and argv[1].lower() == "/c" and Path(argv[0]).name.lower() == "cmd.exe":
        argv = argv[2:]  # the COMSPEC wrapper of a .cmd shim: a Windows update must not make a new, cold cache
    parts = []
    for arg in argv:
        try:
            st = os.stat(arg)
            parts.append(f"{os.path.realpath(arg)}|{st.st_size}|{st.st_mtime_ns}")
        except OSError:
            parts.append(arg)
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:12]


def shared_cache_dir(executable: Sequence[str] | None) -> Path:
    """`XDG_CACHE_HOME` for every OpenCode process PHD starts with this executable (runs and catalogue). Shared and
    persistent, so models.json, ripgrep and plugin packages are fetched once, not per run, and never land in the
    host's cache. Keyed by `install_key` so different installs never overwrite each other's cache."""
    return phd_cache_dir() / "opencode" / "xdg-cache" / install_key(executable)


def catalog_home() -> Path:
    return phd_cache_dir() / "opencode" / "catalog-home"


def _host_models_json() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "opencode" / "models.json"


def _prune_stale_tmp(folder: Path) -> None:
    cutoff = time.time() - STALE_TMP_S
    for tmp in folder.glob("models.json.*.tmp"):
        with contextlib.suppress(OSError):
            if tmp.stat().st_mtime < cutoff:
                tmp.unlink()


def seed_models_cache(cache: Path) -> None:
    """Copy the host's models.json into the shared cache once, if PHD has none yet, and prune refresh temp files
    older than an hour. A cold cache makes opencode fall back to its bundled snapshot, which lacks the newest
    models. The host file is only read."""
    folder = cache / "opencode"
    if folder.is_dir():
        _prune_stale_tmp(folder)
    target = folder / "models.json"
    source = _host_models_json()
    if target.exists() or not source.is_file():
        return
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"models.json.phd-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(source, tmp)  # keeps the mtime: opencode refreshes it itself when it is stale
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def catalog_session(credentials: Credentials | None, identity: str, executable: Sequence[str] | None,
                    notes: list[str]) -> Iterator[tuple[dict[str, str], Path]]:
    """PHD's environment overrides and cwd for `opencode models`. One shared home: the credential travels in the
    environment (`OPENCODE_AUTH_CONTENT`), never on disk, so `identity` needs no folder of its own."""
    home = catalog_home()
    cache = shared_cache_dir(executable)
    dirs = {"XDG_DATA_HOME": home / "data", "XDG_CONFIG_HOME": home / "xdg-config", "XDG_STATE_HOME": home / "state",
            "OPENCODE_CONFIG_DIR": home / "config", "XDG_CACHE_HOME": cache}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    seed_models_cache(cache)
    deps.seed_home(home)  # `opencode models` starts the same per-config-folder install as a run
    env = {k: str(v) for k, v in dirs.items()}
    env.update(npm_env())
    env.update({"OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_AUTOUPDATE": "1",
                "OPENCODE_DISABLE_CLAUDE_CODE": "1", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1"})
    if credentials is not None and credentials.kind == "opencode_auth_file":
        env["OPENCODE_AUTH_CONTENT"] = credentials.opencode_auth_content()
    yield env, home
