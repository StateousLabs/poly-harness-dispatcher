"""Process-launch helpers."""

import os
import re
import shutil
from collections.abc import Sequence
from pathlib import Path

from ..errors import HarnessNotInstalled

_NEEDS_QUOTES = re.compile(r"[\s&|<>^()]")


def shell_join(args: Sequence[str]) -> str:
    """Join arguments into a hook command line.

    Quote only arguments that need it: codex runs hooks through cmd.exe on Windows, and
    `cmd /c` strips a leading quote from the command (CoreDesign §14.2)."""
    return " ".join(f'"{a}"' if _NEEDS_QUOTES.search(a) else a for a in args)


def resolve_argv(argv: Sequence[str]) -> list[str]:
    """Resolve argv[0] on PATH; wrap Windows .cmd/.bat shims in `COMSPEC /c`."""
    first, rest = argv[0], list(argv[1:])
    exe = first if Path(first).is_absolute() and Path(first).exists() else shutil.which(first)
    if exe is None:
        raise HarnessNotInstalled(f"{first!r} was not found on PATH; install it or pass an explicit executable")
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", exe, *rest]
    return [exe, *rest]
