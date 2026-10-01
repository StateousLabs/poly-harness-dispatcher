"""Credentials: how one run authenticates (CoreDesign §2a, §7)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..constants import HARNESSES
from ..errors import MissingCredentials, UnknownHarness

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

    def _read_auth_file(self) -> dict:
        """Read and parse auth file, raising MissingCredentials for any error."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise MissingCredentials(f"{self.path}: failed to read or parse ({e})")
        if not isinstance(data, dict):
            raise MissingCredentials(f"{self.path}: top level must be a JSON object, got {type(data).__name__}")
        return data

    def claude_access_token(self) -> str:
        data = self._read_auth_file()
        token = (data.get("claudeAiOauth") or {}).get("accessToken")
        if not token:
            raise MissingCredentials(f"{self.path} has no claudeAiOauth.accessToken; run `claude auth login`")
        return token

    def opencode_auth_content(self) -> str:
        data = self._read_auth_file()
        if self.providers:
            missing = [p for p in self.providers if p not in data]
            if missing:
                raise MissingCredentials(f"{self.path} has no credentials for providers {missing}")
            data = {k: v for k, v in data.items() if k in self.providers}
        return json.dumps(data)
