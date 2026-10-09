"""The environment PHD hands to every harness process (runs and model catalogue commands)."""

import os

# Host variables that could point a harness at the host's config, data, sessions or credentials.
HOST_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "CODEX_", "OPENAI_", "OPENCODE_", "XDG_")


def clean_env(overrides: dict[str, str]) -> dict[str, str]:
    """The host environment minus every harness/auth variable, plus PHD's own values.

    `HOME` / `USERPROFILE` are kept on purpose: the agent still needs the user's git identity, ssh and tool config.
    """
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(HOST_ENV_PREFIXES)}
    env.update(overrides)
    return env
