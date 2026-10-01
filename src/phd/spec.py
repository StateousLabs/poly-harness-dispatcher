"""Spec (what/where/how), Inputs (the task) and Credentials (CoreDesign §2a, §7)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import AUTONOMY_MODES, HARNESSES, PROJECT_CONFIG_MODES
from .errors import (CwdNotFound, EmptyTask, InputNotFound, InvalidSpec, MissingCredentials,
                     MissingSecret, UnknownHarness, UnsafeAutonomy)
from .placeholders import env_refs
from .stack import Stack

CLAUDE_HOST_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
CODEX_HOST_AUTH = Path.home() / ".codex" / "auth.json"
OPENCODE_HOST_AUTH = Path.home() / ".local" / "share" / "opencode" / "auth.json"


@dataclass(frozen=True)
class Credentials:
    """How one run authenticates. Secrets are read at launch time and injected per run."""
    harness: str
    kind: str
    secret: str | None = field(default=None, repr=False)
    path: Path | None = None
    providers: tuple[str, ...] = ()

    @classmethod
    def claude_oauth_token(cls, token: str) -> Credentials:
        """Long-lived token from `claude setup-token` (preferred: no refresh-token rotation)."""
        return cls("claude", "claude_oauth_token", secret=token)

    @classmethod
    def anthropic_api_key(cls, key: str) -> Credentials:
        return cls("claude", "anthropic_api_key", secret=key)

    @classmethod
    def claude_host_login(cls, path: str | Path | None = None) -> Credentials:
        """Use the host login's current *access token* only; the refresh token is never copied."""
        p = Path(path) if path else CLAUDE_HOST_CREDENTIALS
        if not p.is_file():
            raise MissingCredentials(f"{p} not found; run `claude auth login` or use Credentials.claude_oauth_token()")
        return cls("claude", "claude_host_login", path=p)

    @classmethod
    def codex_auth_file(cls, path: str | Path | None = None) -> Credentials:
        p = Path(path) if path else CODEX_HOST_AUTH
        if not p.is_file():
            raise MissingCredentials(f"{p} not found; run `codex login`")
        return cls("codex", "codex_auth_file", path=p)

    @classmethod
    def opencode_auth_file(cls, path: str | Path | None = None, providers: tuple[str, ...] | list[str] = ()) -> Credentials:
        p = Path(path) if path else OPENCODE_HOST_AUTH
        if not p.is_file():
            raise MissingCredentials(f"{p} not found; run `opencode auth login`")
        return cls("opencode", "opencode_auth_file", path=p, providers=tuple(providers))

    @classmethod
    def from_host(cls, harness: str) -> Credentials:
        makers = {"claude": cls.claude_host_login, "codex": cls.codex_auth_file, "opencode": cls.opencode_auth_file}
        if harness not in makers:
            raise UnknownHarness(f"unknown harness {harness!r}; expected one of {list(HARNESSES)}")
        return makers[harness]()

    def claude_access_token(self) -> str:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        token = (data.get("claudeAiOauth") or {}).get("accessToken")
        if not token:
            raise MissingCredentials(f"{self.path} has no claudeAiOauth.accessToken; run `claude auth login`")
        return token

    def opencode_auth_content(self) -> str:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if self.providers:
            missing = [p for p in self.providers if p not in data]
            if missing:
                raise MissingCredentials(f"{self.path} has no credentials for providers {missing}")
            data = {k: v for k, v in data.items() if k in self.providers}
        return json.dumps(data)


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
        if self.timeout_s <= 0:
            raise InvalidSpec("timeout_s must be positive")
        if self.budget_usd is not None and self.budget_usd <= 0:
            raise InvalidSpec("budget_usd must be positive when set")
        self.stack.check_harness(self.harness)
        needed = env_refs(self.stack.mcp_servers) | env_refs([h.command for h in self.stack.hooks])
        missing = sorted(needed - set(self.secrets))
        if missing:
            raise MissingSecret(f"stack {self.stack.ref} needs secrets {missing}; pass them in Spec.secrets")
