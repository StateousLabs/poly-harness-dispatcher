"""Harness adapters."""

from ..errors import UnknownHarness
from .base import Adapter, MaterializeContext, MaterializedHome
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .opencode import OpencodeAdapter

_ADAPTERS: dict[str, Adapter] = {"claude": ClaudeAdapter(), "codex": CodexAdapter(), "opencode": OpencodeAdapter()}


def get_adapter(harness: str) -> Adapter:
    try:
        return _ADAPTERS[harness]
    except KeyError:
        raise UnknownHarness(f"unknown harness {harness!r}; expected one of {sorted(_ADAPTERS)}") from None


__all__ = ["Adapter", "MaterializeContext", "MaterializedHome", "get_adapter"]
