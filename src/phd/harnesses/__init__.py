"""Harness adapters and the registry that maps a harness name to its adapter."""

from pathlib import Path

from ..errors import UnknownHarness
from .base import Adapter, StreamParser
from .catalog import ModelCatalog
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .opencode import OpencodeAdapter
from .outcome import Outcome

_ADAPTERS: dict[str, Adapter] = {"claude": ClaudeAdapter(), "codex": CodexAdapter(), "opencode": OpencodeAdapter()}


def all_secret_paths(home: Path) -> list[Path]:
    """Every adapter's credential copies for a run home (a runs root can hold runs of every harness)."""
    return [p for adapter in _ADAPTERS.values() for p in adapter.secret_paths(home)]


def get_adapter(harness: str) -> Adapter:
    try:
        return _ADAPTERS[harness]
    except KeyError:
        raise UnknownHarness(f"unknown harness {harness!r}; expected one of {sorted(_ADAPTERS)}") from None


__all__ = ["Adapter", "ModelCatalog", "Outcome", "StreamParser", "all_secret_paths", "get_adapter"]
