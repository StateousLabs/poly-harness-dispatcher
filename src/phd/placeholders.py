"""${stack} and ${env:NAME} placeholders in stack MCP/hook config (CoreDesign §9)."""

import re
from collections.abc import Mapping
from typing import Any

from .errors import MissingSecret

_ENV_REF = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def env_refs(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(env_refs(v) for v in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(env_refs(v) for v in value))
    if isinstance(value, str):
        return set(_ENV_REF.findall(value))
    return set()


def resolve(value: Any, *, stack_dir: str, secrets: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: resolve(v, stack_dir=stack_dir, secrets=secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, stack_dir=stack_dir, secrets=secrets) for v in value]
    if not isinstance(value, str):
        return value

    def substitute(match: re.Match) -> str:
        name = match.group(1)
        if name not in secrets:
            raise MissingSecret(f"${{env:{name}}} is referenced by the stack but missing from Spec.secrets")
        return secrets[name]

    return _ENV_REF.sub(substitute, value).replace("${stack}", stack_dir)
