"""Codex model catalogue: `codex debug models`, run isolated from the host.

With the host environment, `codex debug models` refreshes the host's `~/.codex/models_cache.json`. So it runs with
`CODEX_HOME` set to a PHD-owned, non-temp folder (codex refuses a CODEX_HOME under the temp dir) holding a copy of the
spec's `auth.json`; without auth codex lists only its bundled models. One folder per credential identity, so two
credentials loading at the same time never overwrite each other's `auth.json`."""

import json
from collections.abc import Callable
from pathlib import Path

from ..._util.fs import write_private
from ..._util.paths import phd_cache_dir
from ...spec import Credentials


def load(run: Callable[[list[str]], str]) -> list[str]:
    data = json.loads(run(["codex", "debug", "models"]))
    return [m["slug"] for m in data.get("models", []) if m.get("slug")]


def catalog_env(credentials: Credentials | None, identity: str) -> tuple[dict[str, str], Path]:
    """PHD's environment overrides and cwd for `codex debug models`."""
    home = phd_cache_dir() / "codex" / "catalog-home" / identity
    home.mkdir(parents=True, exist_ok=True)
    if credentials is not None and credentials.kind == "codex_auth_file":
        write_private(home / "auth.json", credentials.path.read_text(encoding="utf-8"))
    return {"CODEX_HOME": str(home)}, home
