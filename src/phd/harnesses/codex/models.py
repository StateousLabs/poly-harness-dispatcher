"""Codex model catalogue: `codex debug models`, run isolated from the host.

With the host environment, `codex debug models` refreshes the host's `~/.codex/models_cache.json`. So each load runs
with a throwaway `CODEX_HOME` under the PHD cache (`catalog-home/<identity>/<uuid>/`; not under the temp dir, which
codex refuses) holding a copy of the spec's `auth.json`; without auth codex lists only its bundled models. The folder,
and with it the credential copy, is deleted as soon as the command finishes."""

import contextlib
import json
import tempfile
import time
import uuid
import warnings
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from ..._util.fs import is_within, robust_rmtree, write_private
from ..._util.paths import phd_cache_dir
from ...errors import MissingCredentials
from ...spec import Credentials

_warned_temp = False
STALE_HOME_S = 600  # a catalogue home this old belongs to a process killed mid-load (the command times out at 60 s)


def load(run: Callable[[list[str]], str]) -> list[str]:
    data = json.loads(run(["codex", "debug", "models"]))
    return [m["slug"] for m in data.get("models", []) if m.get("slug")]


def _read_auth(credentials: Credentials) -> bytes:
    try:
        return credentials.path.read_bytes()  # copied verbatim
    except OSError as e:
        raise MissingCredentials(f"{credentials.path}: cannot read the codex auth file ({e}); run `codex login`") from e


def _warn_if_temp(cache: Path) -> None:
    global _warned_temp
    if not _warned_temp and is_within(cache, Path(tempfile.gettempdir()).resolve()):
        _warned_temp = True
        warnings.warn(f"the PHD cache {cache} is under the temp dir: codex refuses a temp CODEX_HOME, so its model "
                      "catalogue will fail (set PHD_CACHE_DIR elsewhere)", RuntimeWarning, stacklevel=2)


def _sweep_stale_homes(root: Path) -> None:
    """Delete catalogue homes (`<identity>/<uuid>/`, any identity) left by a process killed mid-load."""
    if not root.is_dir():
        return
    cutoff = time.time() - STALE_HOME_S
    for home in root.glob("*/*"):
        with contextlib.suppress(OSError):
            if home.is_dir() and home.stat().st_mtime < cutoff:
                robust_rmtree(home)


@contextlib.contextmanager
def catalog_session(credentials: Credentials | None, identity: str, executable: Sequence[str] | None,
                    notes: list[str]) -> Iterator[tuple[dict[str, str], Path]]:
    """PHD's environment overrides and cwd for one `codex debug models`; the home is removed afterwards. A
    failed removal does not discard a loaded list: it is appended to `notes` (reported as a catalogue warning)."""
    auth = _read_auth(credentials) if credentials is not None and credentials.kind == "codex_auth_file" else None
    cache = phd_cache_dir()
    _warn_if_temp(cache)
    root = cache / "codex" / "catalog-home"
    _sweep_stale_homes(root)
    home = root / identity / uuid.uuid4().hex
    try:
        home.mkdir(parents=True)
        if auth is not None:
            write_private(home / "auth.json", auth)
        yield {"CODEX_HOME": str(home)}, home
    finally:
        if home.exists():
            try:
                robust_rmtree(home)
            except OSError as e:
                notes.append(f"could not remove the codex catalogue home {home} (it holds a copy of auth.json; "
                             f"PHD deletes it on a later load once it is 10 minutes old): {e}")
