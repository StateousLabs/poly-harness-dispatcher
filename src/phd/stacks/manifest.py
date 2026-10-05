"""The stack manifest: `stack.toml` or `phd.stacks.compose(...)`, parsed into one `Manifest`."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ..constants import HARNESSES
from ..errors import StackInvalid

ENV_REF = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_TOP_KEYS = {"name", "version", "description", "harnesses", "default_models", "base", "agent",
             "base_hooks", "skills", "mcp", "hooks"}
_ENTRY_KEYS = {"source", "replace", "headers"}


@dataclass(frozen=True)
class ComponentSource:
    """Where one component comes from.

    Attributes:
        source: An `https://` URL (or `http://localhost` / `http://127.0.0.1`) or an absolute
            local path (relative paths are resolved when the manifest is parsed).
        replace: Deliberately replace an existing component with the same name.
        headers: HTTP headers for this source; `${env:NAME}` is expanded from the environment at
            fetch time. Never written to the store.
    """

    source: str
    replace: bool = False
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_url(self) -> bool:
        """True for an http(s) URL, False for a local path."""
        return self.source.startswith(("https://", "http://"))


@dataclass(frozen=True)
class Manifest:
    """A parsed stack manifest: what to fetch and how to assemble it.

    Attributes mirror the `stack.toml` keys; `path` is the manifest file (or `None` for `compose`).
    """

    name: str
    version: str
    description: str | None = None
    harnesses: tuple[str, ...] | None = None
    default_models: dict[str, str] | None = None
    base: ComponentSource | None = None
    agent: ComponentSource | None = None
    base_hooks: bool = True
    skills: tuple[ComponentSource, ...] = ()
    mcp: tuple[ComponentSource, ...] = ()
    hooks: tuple[ComponentSource, ...] = ()
    path: Path | None = None

    @property
    def ref(self) -> str:
        """`"<name>@<version>"`."""
        return f"{self.name}@{self.version}"


def load_manifest(path: str | Path) -> Manifest:
    """Parse and validate a `stack.toml`. Relative sources resolve against its directory.

    Raises:
        StackInvalid: Unreadable file, invalid TOML, or an invalid / unknown key.
    """
    path = Path(path).expanduser().resolve()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise StackInvalid(f"{path}: cannot read ({e})") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise StackInvalid(f"{path}: invalid TOML ({e})") from e
    return _build(data, where=str(path), root=path.parent, path=path)


def compose(name: str, version: str, *, description: str | None = None, harnesses=None,
            default_models: dict[str, str] | None = None, base=None, agent=None, base_hooks: bool = True,
            skills=(), mcp=(), hooks=(), root: str | Path | None = None) -> Manifest:
    """Build a `Manifest` in code; same fields and rules as `stack.toml`.

    `skills` / `mcp` / `hooks` items are a source string (or path) or a dict
    `{"source": ..., "replace": bool, "headers": {...}}`. Relative paths resolve against `root`
    (default: the current working directory).

    Example:
        ```python
        m = compose("reviewer", "1.3.0", base="https://…/reviewer-1.2.0.tar.gz",
                    skills=["~/.phd/skills/pytest-helpers"])
        ```
    """
    data: dict = {"name": name, "version": version, "base_hooks": base_hooks,
                  "skills": [_entry_dict(s) for s in skills], "mcp": [_entry_dict(s) for s in mcp],
                  "hooks": [_entry_dict(s) for s in hooks]}
    for key, value in (("description", description), ("default_models", default_models),
                       ("base", base), ("agent", agent)):
        if value is not None:
            data[key] = os.fspath(value) if isinstance(value, os.PathLike) else value
    if harnesses is not None:
        data["harnesses"] = list(harnesses)
    base_dir = Path(root).expanduser().resolve() if root is not None else Path.cwd()
    return _build(data, where="compose()", root=base_dir, path=None)


def _entry_dict(item) -> dict:
    if isinstance(item, (str, os.PathLike)):
        return {"source": os.fspath(item)}
    return dict(item)


def _build(data: dict, *, where: str, root: Path, path: Path | None) -> Manifest:
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise StackInvalid(f"{where}: unknown key(s) {unknown}")
    for key in ("name", "version"):
        value = data.get(key)
        if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
            raise StackInvalid(f"{where}: {key!r} must match [A-Za-z0-9][A-Za-z0-9._-]* (got {value!r})")
    description = data.get("description")
    if description is not None and not isinstance(description, str):
        raise StackInvalid(f"{where}: 'description' must be a string")
    harnesses = data.get("harnesses")
    if harnesses is not None:
        if not isinstance(harnesses, list) or not harnesses or any(h not in HARNESSES for h in harnesses):
            raise StackInvalid(f"{where}: 'harnesses' must be a non-empty list drawn from {list(HARNESSES)}")
        harnesses = tuple(harnesses)
    default_models = data.get("default_models")
    if default_models is not None and (not isinstance(default_models, dict)
                                       or not all(isinstance(v, str) for v in default_models.values())):
        raise StackInvalid(f"{where}: 'default_models' must be a table of strings")
    base_hooks = data.get("base_hooks", True)
    if not isinstance(base_hooks, bool):
        raise StackInvalid(f"{where}: 'base_hooks' must be true or false")
    entries = {}
    for key in ("skills", "mcp", "hooks"):
        items = data.get(key, [])
        if not isinstance(items, list):
            raise StackInvalid(f"{where}: {key!r} must be an array of tables ([[{key}]])")
        entries[key] = tuple(_entry(item, f"{where}: {key}[{i}]", root) for i, item in enumerate(items))
    return Manifest(
        name=data["name"], version=data["version"], description=description, harnesses=harnesses,
        default_models=dict(default_models) if default_models is not None else None,
        base=ComponentSource(_source(data["base"], f"{where}: 'base'", root)) if "base" in data else None,
        agent=ComponentSource(_source(data["agent"], f"{where}: 'agent'", root)) if "agent" in data else None,
        base_hooks=base_hooks, skills=entries["skills"], mcp=entries["mcp"], hooks=entries["hooks"], path=path)


def _entry(item, where: str, root: Path) -> ComponentSource:
    if not isinstance(item, dict):
        raise StackInvalid(f"{where}: must be a table with a 'source'")
    unknown = sorted(set(item) - _ENTRY_KEYS)
    if unknown:
        raise StackInvalid(f"{where}: unknown key(s) {unknown}")
    if "source" not in item:
        raise StackInvalid(f"{where}: 'source' is required")
    replace = item.get("replace", False)
    if not isinstance(replace, bool):
        raise StackInvalid(f"{where}: 'replace' must be true or false")
    headers = item.get("headers", {})
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise StackInvalid(f"{where}: 'headers' must be a table of strings")
    return ComponentSource(_source(item["source"], where, root), replace=replace, headers=dict(headers))


def _source(value, where: str, root: Path) -> str:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value.strip():
        raise StackInvalid(f"{where}: source must be a non-empty string")
    if "://" in value:
        parts = urlsplit(value)
        if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")):
            return value
        raise StackInvalid(f"{where}: {parts.scheme}://{parts.hostname or ''}: only https:// URLs (or http://localhost) and local paths are supported")
    try:
        p = Path(value).expanduser()
    except (RuntimeError, ValueError) as e:
        raise StackInvalid(f"{where}: invalid path ({e})") from e
    try:
        return str(p.resolve() if p.is_absolute() else (root / p).resolve())
    except (RuntimeError, ValueError) as e:
        raise StackInvalid(f"{where}: invalid path ({e})") from e
