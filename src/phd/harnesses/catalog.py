"""Model catalogues (CoreDesign §8). A catalogue only proves a model *exists*; whether this
account may use it is known only after launch (error.kind="model_unavailable")."""

import difflib
import hashlib
import os
import subprocess
import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path

from .._util.env import clean_env
from .._util.proc import kill_tree
from .._util.shell import resolve_argv
from ..errors import HarnessNotInstalled, ModelNotSupported, UnknownHarness
from ..spec import Credentials
from .claude import models as claude_models
from .codex import models as codex_models
from .opencode import models as opencode_models

Runner = Callable[[list[str]], str]

_LOADERS: dict[str, Callable[[Runner], list[str]]] = {
    "claude": claude_models.load, "codex": codex_models.load, "opencode": opencode_models.load}
# harnesses with no catalogue command: classify() returns warnings if acceptable, None if unknown
_STATIC_CHECKS: dict[str, Callable[[str], list[str] | None]] = {"claude": claude_models.classify}
# isolated environment overrides and cwd for each catalogue command (never the host's harness folders); a context
# manager so per-load folders (codex's credential copy) are removed when the command ends
_CatalogSession = Callable[[Credentials | None, str, Sequence[str] | None, list[str]],
                           AbstractContextManager[tuple[dict[str, str], Path]]]
_CATALOG_SESSIONS: dict[str, _CatalogSession] = {
    "codex": codex_models.catalog_session, "opencode": opencode_models.catalog_session}
# catalogues that can lag behind what the harness accepts (opencode: a seeded or bundled models.dev snapshot);
# a miss there is a warning, and a truly unknown model fails after launch as error.kind="model_unavailable"
_LAGGING = frozenset({"opencode"})


# Catalogues of codex / opencode, loaded once per process and shared by every ModelCatalog that uses the
# default runner. Key: (harness, executable argv prefix; empty = the CLI found on PATH, credential identity).
# Failures are not cached.
_Key = tuple[str, tuple[str, ...], str]
_PROCESS_CACHE: dict[_Key, list[str]] = {}
_KEY_LOCKS: dict[_Key, threading.Lock] = {}  # one per key: different harnesses load in parallel
_KEY_LOCKS_GUARD = threading.Lock()  # only protects creating entries of _KEY_LOCKS


def credential_identity(credentials: Credentials | None) -> str:
    """A short, one-way identity of a credential for cache keys and folder names: a hash of its kind, file,
    provider filter and secret. Neither the secret nor the path appears in clear. `"none"` without credentials.

    For the file kinds the identity covers the file's *path*, not its contents: re-logging into the same file keeps
    the identity (and the cached list) for the rest of the process."""
    if credentials is None:
        return "none"
    parts = [credentials.harness, credentials.kind, str(credentials.path or ""),
             ",".join(credentials.providers), credentials.secret or ""]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:16]


CATALOG_TIMEOUT_S = 60


def _run_cli(argv: list[str], *, env: dict[str, str], cwd: Path) -> str:
    """Run a catalogue command; on timeout kill its whole process tree (on Windows `cmd.exe /c` would otherwise
    leave the real CLI running, still holding its CODEX_HOME) and re-raise `TimeoutExpired`."""
    group = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(resolve_argv(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env, cwd=cwd,
                            **group)
    try:
        stdout, stderr = proc.communicate(timeout=CATALOG_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} exited {proc.returncode}: {stderr.strip()[:300]}")
    return stdout


class ModelCatalog:
    """Checks that a model name exists for a harness before launch.

    Sources: codex runs `codex debug models` (JSON slugs); opencode runs `opencode models`
    (`provider/model` lines); claude has no catalogue command, so PHD uses built-in aliases
    (`opus`, `sonnet`, `haiku`, `fable`) and known ids, and accepts any other `claude-*` id with a
    warning. Loaded lists are cached per instance; with the default runner they are also cached per
    process (per harness, executable and credential), so repeated `Dispatcher`s run each catalogue command once.

    The default runner never uses the host's harness setup: the command gets the host environment minus every
    `ANTHROPIC_`/`CLAUDE_`/`CODEX_`/`OPENAI_`/`OPENCODE_`/`XDG_` variable, PHD-owned folders under the PHD cache
    (`$PHD_CACHE_DIR`, default `~/.phd/cache`) as its harness home and cwd, and the given credentials. Without
    credentials it still runs isolated, but codex and opencode then list only the models that need no login.
    `models` and `validate` block while a catalogue loads: call them from a worker thread in async code. A catalogue proves only that a model exists;
    whether the account may use it shows up after launch as `error.kind="model_unavailable"`.

    Example:
        ```python
        cat = ModelCatalog()
        cat.validate("claude", "sonnet")          # [] (no warnings)
        cat.models("opencode", spec.credentials)  # runs `opencode models`, isolated
        ```
    """

    def __init__(self, runner: Runner | None = None, *, executables: dict[str, list[str]] | None = None):
        """Create an empty catalogue; nothing is loaded until first use.

        Args:
            runner: Function that runs a CLI argv and returns its stdout. Defaults to a
                subprocess call with a 60 s timeout in an isolated environment; inject one in tests.
                An injected runner receives only the argv (no environment) and is cached per instance only.
            executables: Per-harness argv prefix replacing the CLI name in the catalogue command
                (as `Dispatcher(executables=...)`). Only used with the default runner.
        """
        self._custom_runner = runner is not None
        self._executables = {k: list(v) for k, v in (executables or {}).items()}
        self._runner = runner
        self._cache: dict[tuple[str, str], list[str]] = {}
        self._notes: list[str] = []  # cleanup problems of this instance's loads, reported by the next validate()

    def _default_runner(self, credentials: Credentials | None, identity: str) -> Runner:
        def run(argv: list[str]) -> str:
            prefix = self._executables.get(argv[0])
            with _CATALOG_SESSIONS[argv[0]](credentials, identity, prefix, self._notes) as (overrides, cwd):
                return _run_cli([*prefix, *argv[1:]] if prefix else argv, env=clean_env(overrides), cwd=cwd)
        return run

    def is_static(self, harness: str) -> bool:
        """True when `validate` for `harness` needs no subprocess (claude)."""
        return harness in _STATIC_CHECKS

    def models(self, harness: str, credentials: Credentials | None = None) -> list[str]:
        """Return (and cache) the known model names for `harness`.

        Args:
            harness: Harness name.
            credentials: The login the catalogue command uses (normally `spec.credentials`). The list depends on
                it (opencode lists only free models without one), so it is part of the cache key.

        Raises:
            UnknownHarness: `harness` is not supported.
            HarnessNotInstalled: The CLI is not on PATH (codex / opencode).
            RuntimeError: The catalogue command failed.
            MissingCredentials: The credential file cannot be read.
        """
        identity = credential_identity(credentials)
        local_key = (harness, identity)
        if local_key in self._cache:
            return self._cache[local_key]
        if self._custom_runner or harness in _STATIC_CHECKS:
            self._cache[local_key] = self._load(harness, self._runner or _no_command)
        else:
            key = (harness, tuple(self._executables.get(harness, ())), identity)
            with _KEY_LOCKS_GUARD:
                key_lock = _KEY_LOCKS.setdefault(key, threading.Lock())
            with key_lock:  # one load per key; a second caller then finds it cached
                if key not in _PROCESS_CACHE:
                    _PROCESS_CACHE[key] = self._load(harness, self._default_runner(credentials, identity))
                self._cache[local_key] = _PROCESS_CACHE[key]
        return self._cache[local_key]

    def _load(self, harness: str, run: Runner) -> list[str]:
        try:
            loader = _LOADERS[harness]
        except KeyError:
            raise UnknownHarness(f"unknown harness {harness!r}") from None
        return loader(run)

    def validate(self, harness: str, model: str, credentials: Credentials | None = None) -> list[str]:
        """Check `model` for `harness`; return warnings, raise if it is unknown.

        For codex and opencode, a catalogue that cannot be loaded (CLI missing, command failing,
        unparsable output, timeout, an unusable PHD cache folder) yields a warning instead of an error;
        on timeout the command's whole process tree is killed. A loaded list whose temporary codex home could not
        be removed is kept, with a warning.

        Args:
            harness: Harness name.
            model: Model name as it would go in `Spec.model`. For claude a context suffix such
                as `[1m]` is ignored when matching.
            credentials: The login the catalogue command uses (see `models`).

        Returns:
            Warnings (empty when the model is known).

        Raises:
            ModelNotSupported: The model is not in the catalogue (carries `supported` and a
                closest-match `hint`).
        """
        warnings = self._check(harness, model, credentials)
        notes, self._notes[:] = list(self._notes), []
        return [*warnings, *notes]

    def _check(self, harness: str, model: str, credentials: Credentials | None) -> list[str]:
        static_check = _STATIC_CHECKS.get(harness)
        if static_check is not None:
            warnings = static_check(model)
            if warnings is not None:
                return warnings
            known = self.models(harness, credentials)
            raise ModelNotSupported(harness, model, known, _closest(model, known))
        try:
            known = self.models(harness, credentials)
        except (HarnessNotInstalled, RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
            return [f"could not load the {harness} model catalogue ({e}); model {model!r} was not checked"]
        if model in known:
            return []
        if harness in _LAGGING:
            hint = _closest(model, known)
            return [f"model {model!r} is not in the {harness} model catalogue"
                    + (f" (closest: {hint!r})" if hint else "")
                    + "; the catalogue can lag behind, so the run goes ahead and an unknown model fails as "
                      "error.kind='model_unavailable'"]
        raise ModelNotSupported(harness, model, known, _closest(model, known))


def _no_command(argv: list[str]) -> str:
    raise AssertionError(f"static catalogue ran a command: {argv}")


def _closest(model: str, known: list[str]) -> str | None:
    match = difflib.get_close_matches(model, known, n=1, cutoff=0.5)
    return match[0] if match else None
