"""Model catalogues (CoreDesign §8). A catalogue only proves a model *exists*; whether this
account may use it is known only after launch (error.kind="model_unavailable")."""

import difflib
import json
import re
import subprocess
from collections.abc import Callable

from ._util.shell import resolve_argv
from .errors import HarnessNotInstalled, ModelNotSupported, UnknownHarness

CLAUDE_ALIASES = ("opus", "sonnet", "haiku", "fable")
CLAUDE_KNOWN_IDS = ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5",
                    "claude-sonnet-5", "claude-haiku-4-5", "claude-haiku-4-5-20251001")
_CLAUDE_PATTERN = re.compile(r"claude-[a-z0-9][a-z0-9.-]*")
_CONTEXT_SUFFIX = re.compile(r"\[[0-9]+m\]$")

Runner = Callable[[list[str]], str]


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
        if harness == "claude":
            return [*CLAUDE_ALIASES, *CLAUDE_KNOWN_IDS]
        if harness == "codex":
            data = json.loads(self._run(["codex", "debug", "models"]))
            return [m["slug"] for m in data.get("models", []) if m.get("slug")]
        if harness == "opencode":
            lines = (line.strip() for line in self._run(["opencode", "models"]).splitlines())
            return [line for line in lines if "/" in line and " " not in line]
        raise UnknownHarness(f"unknown harness {harness!r}")

    def validate(self, harness: str, model: str) -> list[str]:
        """Raise ModelNotSupported for an unknown model; otherwise return warnings."""
        if harness == "claude":
            base = _CONTEXT_SUFFIX.sub("", model)
            if base in CLAUDE_ALIASES or base in CLAUDE_KNOWN_IDS:
                return []
            if _CLAUDE_PATTERN.fullmatch(base):
                return [f"claude model {model!r} is not in PHD's known list; it will be tried as-is"]
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
