"""Model catalogues (CoreDesign §8). A catalogue only proves a model *exists*; whether this
account may use it is known only after launch (error.kind="model_unavailable")."""

import difflib
import subprocess
import threading
from collections.abc import Callable

from .._util.shell import resolve_argv
from ..errors import HarnessNotInstalled, ModelNotSupported, UnknownHarness
from .claude import models as claude_models
from .codex import models as codex_models
from .opencode import models as opencode_models

Runner = Callable[[list[str]], str]

_LOADERS: dict[str, Callable[[Runner], list[str]]] = {
    "claude": claude_models.load, "codex": codex_models.load, "opencode": opencode_models.load}
# harnesses with no catalogue command: classify() returns warnings if acceptable, None if unknown
_STATIC_CHECKS: dict[str, Callable[[str], list[str] | None]] = {"claude": claude_models.classify}


# Catalogues of codex / opencode, loaded once per process and shared by every ModelCatalog that uses the
# default runner. Key: (harness, executable argv prefix; empty = the CLI found on PATH). Failures are not cached.
_PROCESS_CACHE: dict[tuple[str, tuple[str, ...]], list[str]] = {}
_PROCESS_LOCK = threading.Lock()


def _run_cli(argv: list[str]) -> str:
    proc = subprocess.run(resolve_argv(argv), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return proc.stdout


class ModelCatalog:
    """Checks that a model name exists for a harness before launch.

    Sources: codex runs `codex debug models` (JSON slugs); opencode runs `opencode models`
    (`provider/model` lines); claude has no catalogue command, so PHD uses built-in aliases
    (`opus`, `sonnet`, `haiku`, `fable`) and known ids, and accepts any other `claude-*` id with a
    warning. Loaded lists are cached per instance; with the default runner they are also cached per
    process (per harness and executable), so repeated `Dispatcher`s run each catalogue command once.
    `models` and `validate` block while a catalogue loads: call them from a worker thread in async code. A catalogue proves only that a model exists;
    whether the account may use it shows up after launch as `error.kind="model_unavailable"`.

    Example:
        ```python
        cat = ModelCatalog()
        cat.validate("claude", "sonnet")          # [] (no warnings)
        cat.models("opencode")                    # runs `opencode models`
        ```
    """

    def __init__(self, runner: Runner | None = None, *, executables: dict[str, list[str]] | None = None):
        """Create an empty catalogue; nothing is loaded until first use.

        Args:
            runner: Function that runs a CLI argv and returns its stdout. Defaults to a
                subprocess call with a 60 s timeout; inject one in tests. An injected runner is
                cached per instance only.
            executables: Per-harness argv prefix replacing the CLI name in the catalogue command
                (as `Dispatcher(executables=...)`). Only used with the default runner.
        """
        self._custom_runner = runner is not None
        self._executables = {k: list(v) for k, v in (executables or {}).items()}
        self._run = runner or self._run_default
        self._cache: dict[str, list[str]] = {}

    def _run_default(self, argv: list[str]) -> str:
        prefix = self._executables.get(argv[0])
        return _run_cli([*prefix, *argv[1:]] if prefix else argv)

    def is_static(self, harness: str) -> bool:
        """True when `validate` for `harness` needs no subprocess (claude)."""
        return harness in _STATIC_CHECKS

    def models(self, harness: str) -> list[str]:
        """Return (and cache) the known model names for `harness`.

        Raises:
            UnknownHarness: `harness` is not supported.
            HarnessNotInstalled: The CLI is not on PATH (codex / opencode).
            RuntimeError: The catalogue command failed.
        """
        if harness in self._cache:
            return self._cache[harness]
        if self._custom_runner or harness in _STATIC_CHECKS:
            self._cache[harness] = self._load(harness)
        else:
            key = (harness, tuple(self._executables.get(harness, ())))
            with _PROCESS_LOCK:  # one load at a time; a second caller then finds it cached
                if key not in _PROCESS_CACHE:
                    _PROCESS_CACHE[key] = self._load(harness)
                self._cache[harness] = _PROCESS_CACHE[key]
        return self._cache[harness]

    def _load(self, harness: str) -> list[str]:
        try:
            loader = _LOADERS[harness]
        except KeyError:
            raise UnknownHarness(f"unknown harness {harness!r}") from None
        return loader(self._run)

    def validate(self, harness: str, model: str) -> list[str]:
        """Check `model` for `harness`; return warnings, raise if it is unknown.

        For codex and opencode, a catalogue that cannot be loaded (CLI missing, command failing,
        unparsable output, timeout) yields a warning instead of an error.

        Args:
            harness: Harness name.
            model: Model name as it would go in `Spec.model`. For claude a context suffix such
                as `[1m]` is ignored when matching.

        Returns:
            Warnings (empty when the model is known).

        Raises:
            ModelNotSupported: The model is not in the catalogue (carries `supported` and a
                closest-match `hint`).
        """
        static_check = _STATIC_CHECKS.get(harness)
        if static_check is not None:
            warnings = static_check(model)
            if warnings is not None:
                return warnings
            known = self.models(harness)
            raise ModelNotSupported(harness, model, known, _closest(model, known))
        try:
            known = self.models(harness)
        except (HarnessNotInstalled, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            return [f"could not load the {harness} model catalogue ({e}); model {model!r} was not checked"]
        if model in known:
            return []
        raise ModelNotSupported(harness, model, known, _closest(model, known))


def _closest(model: str, known: list[str]) -> str | None:
    match = difflib.get_close_matches(model, known, n=1, cutoff=0.5)
    return match[0] if match else None
