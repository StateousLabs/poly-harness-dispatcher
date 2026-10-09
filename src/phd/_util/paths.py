"""PHD-owned folders outside any run home."""

import os
from pathlib import Path


def phd_cache_dir() -> Path:
    """The shared PHD cache: `$PHD_CACHE_DIR`, else `~/.phd/cache`. Resolved on every call (tests and child
    processes may set the variable). Holds the isolated model catalogue homes and OpenCode's shared XDG cache."""
    return Path(os.environ.get("PHD_CACHE_DIR") or Path.home() / ".phd" / "cache").expanduser().resolve()
