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
    """How one run authenticates.

    Build it with one of the classmethod constructors rather than the raw dataclass. The value
    names a credential *kind*; file-based kinds only remember the path, and the file is read when
    the run is materialized (`Dispatcher.plan` / `start` / `spawn`) and again on every resume.
    Each run gets the credential injected into its own environment or config home; the host's
    login files are never modified.

    Only `kind` is recorded in `RunResult.credential_kind`; `secret` is excluded from `repr()`.

    Attributes:
        harness: The harness this credential is for (`"claude"`, `"codex"` or `"opencode"`).
            Must equal `Spec.harness`, otherwise `Spec` raises `MissingCredentials`.
        kind: One of `"claude_oauth_token"`, `"anthropic_api_key"`, `"claude_host_login"`,
            `"codex_auth_file"`, `"opencode_auth_file"`.
        secret: The token or API key for the token kinds; `None` for file kinds.
        path: The auth file for the file kinds; `None` for token kinds.
        providers: For `opencode_auth_file` only: the provider entries to copy into the run
            (empty means all entries).

    Example:
        ```python
        from phd import Credentials

        creds = Credentials.from_host("claude")            # ~/.claude/.credentials.json
        creds = Credentials.claude_oauth_token(token)      # token from `claude setup-token`
        ```
    """
    harness: str
    kind: str
    secret: str | None = field(default=None, repr=False)
    path: Path | None = None
    providers: tuple[str, ...] = ()

    @classmethod
    def claude_oauth_token(cls, token: str) -> Credentials:
        """Use a long-lived OAuth token from `claude setup-token`.

        Preferred for claude: no refresh-token rotation, and it works on macOS where the host
        login lives in the Keychain. Injected as `CLAUDE_CODE_OAUTH_TOKEN`.

        Args:
            token: The token string.

        Returns:
            A `claude_oauth_token` credential.
        """
        return cls("claude", "claude_oauth_token", secret=token)

    @classmethod
    def anthropic_api_key(cls, key: str) -> Credentials:
        """Use an Anthropic API key for claude. Injected as `ANTHROPIC_API_KEY`.

        Args:
            key: The API key.

        Returns:
            An `anthropic_api_key` credential.
        """
        return cls("claude", "anthropic_api_key", secret=key)

    @classmethod
    def claude_host_login(cls, path: str | Path | None = None) -> Credentials:
        """Use the host's `claude auth login` session.

        The file is copied verbatim (byte for byte, refresh token included) into the run home as
        `.credentials.json` (mode 0600 on POSIX) at materialize time and again on resume, and deleted
        when the turn ends. PHD never writes to `path`. This is the full login that `Spec.chrome`
        (`claude --chrome`) needs; the token kinds do not work with it.

        Args:
            path: The credentials file. Defaults to `~/.claude/.credentials.json`.

        Returns:
            A `claude_host_login` credential.

        Raises:
            MissingCredentials: The file does not exist (for example on macOS, where the login is
                kept in the Keychain; use `claude_oauth_token` there, which cannot drive Chrome).
        """
        p = Path(path) if path else CLAUDE_HOST_CREDENTIALS
        if not p.is_file():
            raise MissingCredentials(f"{p} not found; run `claude auth login` or use Credentials.claude_oauth_token()")
        return cls("claude", "claude_host_login", path=p)

    @classmethod
    def codex_auth_file(cls, path: str | Path | None = None) -> Credentials:
        """Use a codex `auth.json` (from `codex login`).

        The file is copied verbatim (byte for byte) into the run home as `auth.json` (mode 0600 on POSIX) at
        materialize time and again on resume, and deleted when the turn ends. PHD never writes to `path`.

        Args:
            path: The auth file. Defaults to `~/.codex/auth.json`.

        Returns:
            A `codex_auth_file` credential.

        Raises:
            MissingCredentials: The file does not exist.
        """
        p = Path(path) if path else CODEX_HOST_AUTH
        if not p.is_file():
            raise MissingCredentials(f"{p} not found; run `codex login`")
        return cls("codex", "codex_auth_file", path=p)

    @classmethod
    def opencode_auth_file(cls, path: str | Path | None = None, providers: tuple[str, ...] | list[str] = ()) -> Credentials:
        """Use an opencode `auth.json` (from `opencode auth login`).

        At materialize time the file is parsed, filtered to `providers` and passed to opencode as
        the `OPENCODE_AUTH_CONTENT` environment variable; no auth file is written to the run home.

        Args:
            path: The auth file. Defaults to `~/.local/share/opencode/auth.json`.
            providers: Provider keys to keep (for example `["opencode"]`). Empty keeps all.

        Returns:
            An `opencode_auth_file` credential.

        Raises:
            MissingCredentials: The file does not exist.
        """
        p = Path(path) if path else OPENCODE_HOST_AUTH
        if not p.is_file():
            raise MissingCredentials(f"{p} not found; run `opencode auth login`")
        return cls("opencode", "opencode_auth_file", path=p, providers=tuple(providers))

    @classmethod
    def from_host(cls, harness: str) -> Credentials:
        """Use the host CLI's own login at its default location.

        Equivalent to `claude_host_login()`, `codex_auth_file()` or `opencode_auth_file()`.

        Args:
            harness: `"claude"`, `"codex"` or `"opencode"`.

        Returns:
            The matching credential.

        Raises:
            UnknownHarness: `harness` is not one of the three.
            MissingCredentials: The host login file does not exist.
        """
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

    def opencode_auth_content(self) -> str:
        """Return the auth file as JSON text, filtered to `providers` (used for `opencode_auth_file`).

        Raises:
            MissingCredentials: The file cannot be read or parsed, or lacks a requested provider.
        """
        data = self._read_auth_file()
        if self.providers:
            missing = [p for p in self.providers if p not in data]
            if missing:
                raise MissingCredentials(f"{self.path} has no credentials for providers {missing}")
            data = {k: v for k, v in data.items() if k in self.providers}
        return json.dumps(data)
