"""Spec (what/where/how) and Inputs (the task) (CoreDesign §2a)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..constants import AUTONOMY_MODES, HARNESSES, PROJECT_CONFIG_MODES
from ..errors import (CwdNotFound, EmptyTask, InputNotFound, InvalidSpec, MissingCredentials,
                      MissingSecret, UnknownHarness, UnsafeAutonomy)
from .credentials import Credentials
from .placeholders import env_refs
from .stack import Stack


@dataclass(frozen=True)
class Inputs:
    brief: Path | None = None
    msg: str | None = None
    rules: Path | None = None

    def __post_init__(self) -> None:
        for name in ("brief", "rules"):
            value = getattr(self, name)
            if value is not None:
                p = Path(value).expanduser().resolve()
                if not p.is_file():
                    raise InputNotFound(f"{name} file not found: {p}")
                object.__setattr__(self, name, p)
        if self.brief is None and not (self.msg and self.msg.strip()):
            raise EmptyTask("at least one of brief or msg is required")


@dataclass
class Spec:
    harness: str
    model: str
    stack: Stack
    cwd: Path
    credentials: Credentials
    harness_config: dict[str, Any] = field(default_factory=dict)
    project_config: str = "ignore"
    autonomy: str = "restricted"
    unsafe: bool = False
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    timeout_s: float = 1800.0
    budget_usd: float | None = None
    output_schema: dict | None = None
    resumable: bool = True
    harness_builtins: bool = False

    def __post_init__(self) -> None:
        if self.harness not in HARNESSES:
            raise UnknownHarness(f"unknown harness {self.harness!r}; expected one of {list(HARNESSES)}")
        if not isinstance(self.model, str) or not self.model.strip():
            raise InvalidSpec("model must be a non-empty string")
        self.cwd = Path(self.cwd).expanduser().resolve()
        if not self.cwd.is_dir():
            raise CwdNotFound(f"cwd is not a directory: {self.cwd}")
        if self.project_config not in PROJECT_CONFIG_MODES:
            raise InvalidSpec(f"project_config must be one of {list(PROJECT_CONFIG_MODES)}, got {self.project_config!r}")
        if self.autonomy not in AUTONOMY_MODES:
            raise InvalidSpec(f"autonomy must be one of {list(AUTONOMY_MODES)}, got {self.autonomy!r}")
        if self.autonomy == "full" and not self.unsafe:
            raise UnsafeAutonomy("autonomy='full' bypasses every harness permission check and PHD has no "
                                 "sandbox yet; pass unsafe=True to acknowledge")
        if self.credentials.harness != self.harness:
            raise MissingCredentials(f"credentials are for {self.credentials.harness!r}, spec harness is {self.harness!r}")
        if not isinstance(self.harness_builtins, bool):
            raise InvalidSpec(f"harness_builtins must be a bool, got {self.harness_builtins!r}")
        if self.timeout_s <= 0:
            raise InvalidSpec("timeout_s must be positive")
        if self.budget_usd is not None and self.budget_usd <= 0:
            raise InvalidSpec("budget_usd must be positive when set")
        self.stack.check_harness(self.harness)
        needed = env_refs(self.stack.mcp_servers) | env_refs([h.command for h in self.stack.hooks])
        missing = sorted(needed - set(self.secrets))
        if missing:
            raise MissingSecret(f"stack {self.stack.ref} needs secrets {missing}; pass them in Spec.secrets")
