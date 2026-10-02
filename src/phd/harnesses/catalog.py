"""Model catalogues (CoreDesign §8). A catalogue only proves a model *exists*; whether this
account may use it is known only after launch (error.kind="model_unavailable")."""

import difflib
import subprocess
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
    warning. Loaded lists are cached per instance. A catalogue proves only that a model exists;
    whether the account may use it shows up after launch as `error.kind="model_unavailable"`.

    Example:
        ```python
        cat = ModelCatalog()
        cat.validate("claude", "sonnet")          # [] (no warnings)
        cat.models("opencode")                    # runs `opencode models`
        ```
    """

    def __init__(self, runner: Runner | None = None):
        """Create an empty catalogue; nothing is loaded until first use.

        Args:
            runner: Function that runs a CLI argv and returns its stdout. Defaults to a
                subprocess call with a 60 s timeout; inject one in tests.
        """
        self._run = runner or _run_cli
        self._cache: dict[str, list[str]] = {}

    def models(self, harness: str) -> list[str]:
        """Return (and cache) the known model names for `harness`.

        Raises:
            UnknownHarness: `harness` is not supported.
            HarnessNotInstalled: The CLI is not on PATH (codex / opencode).
            RuntimeError: The catalogue command failed.
        """
        if harness not in self._cache:
            self._cache[harness] = self._load(harness)
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
