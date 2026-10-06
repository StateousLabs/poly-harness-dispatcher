"""Names that become folder names: portable on Windows, macOS and Linux."""

from __future__ import annotations

import re

SAFE_NAME_RULE = ("letters, digits, '.', '_' and '-', starting with a letter or digit, not ending in '.', "
                  "and not a Windows device name")
_SAFE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_RESERVED_DEVICE = re.compile(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", re.IGNORECASE)  # Windows device names


def is_safe_part(part: object) -> bool:
    """True if `part` is a string that is safe as one folder name (see `SAFE_NAME_RULE`)."""
    return (isinstance(part, str) and bool(_SAFE_PART.fullmatch(part)) and not part.endswith(".")
            and not _RESERVED_DEVICE.fullmatch(part))
