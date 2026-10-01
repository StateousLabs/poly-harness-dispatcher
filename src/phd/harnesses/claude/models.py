"""Claude model names. Claude has no catalogue command, so PHD keeps an alias/known-id list and a
pattern for ids it has not heard of (CoreDesign §8)."""

import re
from collections.abc import Callable

CLAUDE_ALIASES = ("opus", "sonnet", "haiku", "fable")
CLAUDE_KNOWN_IDS = ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5",
                    "claude-sonnet-5", "claude-haiku-4-5", "claude-haiku-4-5-20251001")
_CLAUDE_PATTERN = re.compile(r"claude-[a-z0-9][a-z0-9.-]*")
_CONTEXT_SUFFIX = re.compile(r"\[[0-9]+m\]$")


def load(run: Callable[[list[str]], str]) -> list[str]:
    return [*CLAUDE_ALIASES, *CLAUDE_KNOWN_IDS]


def validate(model: str) -> list[str] | None:
    """Warnings if the model is acceptable (known, or id-shaped and tried as-is); None if unknown."""
    base = _CONTEXT_SUFFIX.sub("", model)
    if base in CLAUDE_ALIASES or base in CLAUDE_KNOWN_IDS:
        return []
    if _CLAUDE_PATTERN.fullmatch(base):
        return [f"claude model {model!r} is not in PHD's known list; it will be tried as-is"]
    return None
