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


def _run_cli(argv: list[str]) -> str:
    proc = subprocess.run(resolve_argv(argv), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return proc.stdout


class ModelCatalog:
    def __init__(self, runner: Runner | None = None):
        self._run = runner or _run_cli
        self._cache: dict[str, list[str]] = {}

    def models(self, harness: str) -> list[str]:
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
        """Raise ModelNotSupported for an unknown model; otherwise return warnings."""
        if harness == "claude":
            warnings = claude_models.validate(model)
            if warnings is not None:
                return warnings
            known = self.models("claude")
            raise ModelNotSupported("claude", model, known, _closest(model, known))
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
