"""npm settings for every harness process PHD starts (runs of all harnesses, and the OpenCode catalogue).

The agent's own `npm` commands, and opencode's embedded npm (`@npmcli/config`, which reads `npm_config_*`, the user's
`~/.npmrc` and the global npmrc), get a PHD-owned npm cache and an empty PHD-owned npmrc as user and global config.
So no run writes the host npm cache or reads the host's npm config, as in a future sandbox where none exists. A
project `.npmrc` in the agent's cwd still applies; private registries need a stack-level npmrc (planned)."""

import contextlib

from .paths import phd_cache_dir


def npm_env() -> dict[str, str]:
    """`npm_config_cache` / `npm_config_userconfig` / `npm_config_globalconfig`, each also upper-case with the same
    value: on Windows the host's variable arrives upper-case and must be overridden, not doubled."""
    base = phd_cache_dir() / "npm"
    cache, rc = base / "cache", base / "npmrc"
    cache.mkdir(parents=True, exist_ok=True)
    if not rc.exists():
        with contextlib.suppress(FileExistsError):
            rc.open("x", encoding="utf-8").close()
    env = {}
    for name, value in (("cache", cache), ("userconfig", rc), ("globalconfig", rc)):
        env[f"npm_config_{name}"] = env[f"NPM_CONFIG_{name.upper()}"] = str(value)
    return env
