"""OpenCode model catalogue: `opencode models`, run isolated from the host.

`opencode models` bootstraps a full instance: it opens `$XDG_DATA_HOME/opencode/opencode.db` and registers its cwd
as a project, loads user and project config (and their plugins) and refreshes `$XDG_CACHE_HOME/opencode/models.json`.
So the catalogue gets its own PHD-owned home and cwd, the spec's credentials, and the shared PHD XDG cache that runs
also use (`shared_cache_dir`)."""

import os
import shutil
from collections.abc import Callable
from pathlib import Path

from ..._util.paths import phd_cache_dir
from ...spec import Credentials


def load(run: Callable[[list[str]], str]) -> list[str]:
    lines = (line.strip() for line in run(["opencode", "models"]).splitlines())
    return [line for line in lines if "/" in line and " " not in line]


def shared_cache_dir() -> Path:
    """`XDG_CACHE_HOME` for every OpenCode process PHD starts (runs and catalogue). It is shared and persistent,
    so models.json and the ripgrep download are fetched once, not per run, and never land in the host's cache."""
    return phd_cache_dir() / "opencode" / "xdg-cache"


def catalog_home() -> Path:
    return phd_cache_dir() / "opencode" / "catalog-home"


def _host_models_json() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "opencode" / "models.json"


def seed_models_cache() -> None:
    """Copy the host's models.json into the shared cache once, if PHD has none yet. A cold cache makes opencode
    fall back to its bundled snapshot, which lacks the newest models. The host file is only read."""
    target = shared_cache_dir() / "opencode" / "models.json"
    source = _host_models_json()
    if target.exists() or not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"models.json.phd-{os.getpid()}.tmp")
    try:
        shutil.copy2(source, tmp)  # keeps the mtime: opencode refreshes it itself when it is stale
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)


def catalog_env(credentials: Credentials | None, identity: str) -> tuple[dict[str, str], Path]:
    """PHD's environment overrides and cwd for `opencode models`. One shared home: the credential travels in the
    environment (`OPENCODE_AUTH_CONTENT`), so `identity` needs no folder of its own."""
    home = catalog_home()
    dirs = {"XDG_DATA_HOME": home / "data", "XDG_CONFIG_HOME": home / "xdg-config", "XDG_STATE_HOME": home / "state",
            "OPENCODE_CONFIG_DIR": home / "config", "XDG_CACHE_HOME": shared_cache_dir()}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    seed_models_cache()
    env = {k: str(v) for k, v in dirs.items()}
    env.update({"OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_AUTOUPDATE": "1",
                "OPENCODE_DISABLE_CLAUDE_CODE": "1", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1"})
    if credentials is not None and credentials.kind == "opencode_auth_file":
        env["OPENCODE_AUTH_CONTENT"] = credentials.opencode_auth_content()
    return env, home
