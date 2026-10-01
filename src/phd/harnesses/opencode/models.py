"""OpenCode model catalogue: `opencode models`."""

from collections.abc import Callable


def load(run: Callable[[list[str]], str]) -> list[str]:
    lines = (line.strip() for line in run(["opencode", "models"]).splitlines())
    return [line for line in lines if "/" in line and " " not in line]
