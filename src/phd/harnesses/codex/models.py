"""Codex model catalogue: `codex debug models`."""

import json
from collections.abc import Callable


def load(run: Callable[[list[str]], str]) -> list[str]:
    data = json.loads(run(["codex", "debug", "models"]))
    return [m["slug"] for m in data.get("models", []) if m.get("slug")]
