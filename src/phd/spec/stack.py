"""Harness-neutral agent stack (CoreDesign §2b): one identity plus skills, MCP servers and
neutral hooks, loaded from a config/ directory. A stack is read-only; every run gets its own
config home built from it."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..constants import HARNESSES, NEUTRAL_EVENTS
from ..errors import HookUnsupported, StackIncompatible, StackInvalid


@dataclass(frozen=True)
class HookDef:
    """One neutral hook from `hooks/hooks.json`.

    Attributes:
        event: `"session_start"`, `"pre_tool"`, `"post_tool"` or `"stop"`.
        command: A shell command line. May contain `${stack}` (the run's copy of the stack,
            shell-quoted) and `${env:NAME}` (from `Spec.secrets`).
        tool: Neutral tool to match (`"shell"`, `"read"`, `"write"`, `"edit"`, `"web_fetch"`,
            `"web_search"`, `"mcp:<server>[/<tool>]"`), from the JSON `match.tool`; `None`
            matches every tool.
        timeout_s: Seconds the hook command may run. Default 30.
    """

    event: str
    command: str
    tool: str | None = None
    timeout_s: float = 30.0

    def as_dict(self) -> dict:
        """Return the hook as a plain dict with keys `event`, `command`, `tool`, `timeout_s`."""
        return {"event": self.event, "command": self.command, "tool": self.tool, "timeout_s": self.timeout_s}


@dataclass(frozen=True)
class Stack:
    """A harness-neutral agent: one identity plus skills, MCP servers and hooks.

    Load it from a `config/` directory with `Stack.load`. A stack is read-only: every run copies
    it into its own run home (`home/stack/`) and builds the harness config from it, so many runs
    can share one stack. Layout and validation rules: `docs/concepts/stacks.md`.

    Attributes:
        root: Absolute path of the stack directory.
        name: `stack.json` `name`.
        version: `stack.json` `version`.
        description: `stack.json` `description` (empty if absent).
        harnesses: `stack.json` `harnesses` compatibility list, or `None` for any harness.
        default_models: `stack.json` `default_models` (harness -> model). Informational only:
            PHD never reads it; `Spec.model` is always required.
        agent_name: File stem of the single `agents/*.md` file.
        identity: Text of that file; becomes the start of the system prompt.
        skills: Skill directories under `skills/` (each has a `SKILL.md`), sorted by name.
        mcp_servers: `.mcp.json` `mcpServers`, unresolved (placeholders intact).
        hooks: Parsed `hooks/hooks.json` entries.
        store: Where the stack lives in the stack store, or None for Stack.load(dir).
    """

    root: Path
    name: str
    version: str
    description: str
    harnesses: tuple[str, ...] | None
    default_models: dict[str, str]
    agent_name: str
    identity: str
    skills: tuple[Path, ...]
    mcp_servers: dict[str, dict]
    hooks: tuple[HookDef, ...]
    store: StoreEntry | None = None

    @property
    def ref(self) -> str:
        """`"<name>@<version>"`; recorded as `RunResult.stack`."""
        return f"{self.name}@{self.version}"

    @classmethod
    def load(cls, root: str | Path) -> Stack:
        """Load and validate a stack directory.

        Args:
            root: The stack directory (`~` is expanded). Only directories are supported.

        Returns:
            The loaded stack.

        Raises:
            StackInvalid: Missing or malformed `stack.json`; not exactly one `agents/*.md`; a
                skill folder without `SKILL.md`; malformed `.mcp.json` or `hooks/hooks.json`.
            HookUnsupported: A hook names an event outside the neutral set.

        Example:
            ```python
            stack = Stack.load("tests/fixtures/stack")
            stack.ref            # 'fixture-helper@0.1.0'
            ```
        """
        root = Path(root).expanduser().resolve()
        meta = _read_json(root / "stack.json", required=True)
        for key in ("name", "version"):
            if not isinstance(meta.get(key), str) or not meta[key].strip():
                raise StackInvalid(f"{root / 'stack.json'}: {key!r} must be a non-empty string")

        harnesses = meta.get("harnesses")
        if harnesses is not None:
            if not isinstance(harnesses, list) or not harnesses or any(h not in HARNESSES for h in harnesses):
                raise StackInvalid(f"{root / 'stack.json'}: 'harnesses' must be a non-empty list drawn from {list(HARNESSES)}")
            harnesses = tuple(harnesses)

        default_models = meta.get("default_models", {})
        if not isinstance(default_models, dict):
            raise StackInvalid(f"{root / 'stack.json'}: 'default_models' must be an object")
        for key, val in default_models.items():
            if not isinstance(val, str):
                raise StackInvalid(f"{root / 'stack.json'}: 'default_models' values must be strings")

        agents_dir = root / "agents"
        agents = sorted(agents_dir.glob("*.md")) if agents_dir.is_dir() else []
        if len(agents) != 1:
            raise StackInvalid(f"{agents_dir}: must contain exactly one agent .md file, found {len(agents)}")

        skills: list[Path] = []
        skills_dir = root / "skills"
        if skills_dir.is_dir():
            for d in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
                if not (d / "SKILL.md").is_file():
                    raise StackInvalid(f"{d}: skill folder has no SKILL.md")
                skills.append(d)

        mcp_data = _read_json(root / ".mcp.json")
        mcp_servers_raw = mcp_data.get("mcpServers", {})
        if not isinstance(mcp_servers_raw, dict):
            raise StackInvalid(f"{root / '.mcp.json'}: 'mcpServers' must be an object")
        mcp = mcp_servers_raw
        for name, server in mcp.items():
            _check_mcp(root, name, server)

        hooks_data = _read_json(root / "hooks" / "hooks.json")
        hooks_raw = hooks_data.get("hooks", [])
        if not isinstance(hooks_raw, list):
            raise StackInvalid(f"{root / 'hooks' / 'hooks.json'}: 'hooks' must be an array")
        hooks = tuple(_parse_hook(root, h) for h in hooks_raw)

        return cls(root=root, name=meta["name"], version=meta["version"],
                   description=meta.get("description", ""), harnesses=harnesses,
                   default_models=default_models,
                   agent_name=agents[0].stem, identity=agents[0].read_text(encoding="utf-8"),
                   skills=tuple(skills), mcp_servers=mcp, hooks=hooks)

    def check_harness(self, harness: str) -> None:
        """Raise `StackIncompatible` if `harnesses` is set and excludes `harness`."""
        if self.harnesses is not None and harness not in self.harnesses:
            raise StackIncompatible(f"stack {self.ref} supports {list(self.harnesses)}, not {harness!r}")

    def content_hash(self) -> str:
        """Return a SHA-256 over every file in the stack, as `"sha256:<hex>"`.

        Files are visited in sorted order; each contributes its POSIX relative path and raw bytes
        (no line-ending normalization). Skipped, relative to the stack root: anything named `.git`
        or `__pycache__` (a folder, so its files, or a file) and `*.pyc` files. These are what stack
        assembly leaves out when it copies; other dot-folders and dot-files such as `.mcp.json` are
        hashed. The dispatcher computes it at plan time and records it as `RunResult.stack_hash`.
        """
        return hash_tree(self.root)


def hash_tree(root: Path, *, ignore_meta: tuple[str, ...] = ()) -> str:
    """SHA-256 over a stack directory, as `"sha256:<hex>"`.

    Files are visited in sorted order; each contributes its POSIX relative path and bytes. Skipped
    (relative to `root`): anything named `.git` or `__pycache__` (folder or file) and `*.pyc`: what stack assembly
    does not copy. Everything else is hashed, dot-folders included. With `ignore_meta`, `stack.json` is hashed as
    canonical JSON without those keys (used for the store's identity hash).
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in Path(root).rglob("*") if p.is_file()):
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts or ".git" in rel.parts:  # a .git *file* (worktree, submodule) too
            continue
        if rel.suffix == ".pyc":
            continue
        data = path.read_bytes()
        if ignore_meta and rel.as_posix() == "stack.json":
            meta = json.loads(data.decode("utf-8"))
            data = json.dumps({k: v for k, v in meta.items() if k not in ignore_meta}, sort_keys=True).encode("utf-8")
        digest.update(rel.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _read_json(path: Path, required: bool = False) -> dict:
    if not path.is_file():
        if required:
            raise StackInvalid(f"{path}: file is missing")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise StackInvalid(f"{path}: invalid JSON ({e})") from e
    if not isinstance(data, dict):
        raise StackInvalid(f"{path}: top level must be a JSON object")
    return data


def _check_mcp(root: Path, name: str, server: object) -> None:
    where = f"{root / '.mcp.json'}: server {name!r}"
    if not isinstance(server, dict):
        raise StackInvalid(f"{where} must be an object")
    kind = server.get("type", "stdio")
    if kind == "stdio":
        if not isinstance(server.get("command"), str):
            raise StackInvalid(f"{where}: stdio server needs a 'command' string")
    elif kind == "http":
        if not isinstance(server.get("url"), str):
            raise StackInvalid(f"{where}: http server needs a 'url' string")
    else:
        raise StackInvalid(f"{where}: unknown type {kind!r} (expected 'stdio' or 'http')")


def _parse_hook(root: Path, hook: object) -> HookDef:
    where = root / "hooks" / "hooks.json"
    if not isinstance(hook, dict) or not isinstance(hook.get("command"), str):
        raise StackInvalid(f"{where}: every hook needs a 'command' string")
    if hook.get("event") not in NEUTRAL_EVENTS:
        raise HookUnsupported(f"{where}: unknown hook event {hook.get('event')!r}; expected one of {list(NEUTRAL_EVENTS)}")

    # Validate match field if present
    match = hook.get("match")
    if match is not None and not isinstance(match, dict):
        raise StackInvalid(f"{where}: 'match' must be an object if present")

    # Validate timeout_s
    timeout_s_raw = hook.get("timeout_s", 30)
    try:
        timeout_s = float(timeout_s_raw)
    except (TypeError, ValueError):
        raise StackInvalid(f"{where}: 'timeout_s' must be numeric, got {timeout_s_raw!r}")

    return HookDef(event=hook["event"], command=hook["command"],
                   tool=(match or {}).get("tool") if match is not None else None,
                   timeout_s=timeout_s)


@dataclass(frozen=True)
class StoreEntry:
    """Where a stack lives in the PHD stack store (`phd.stacks`).

    Attributes:
        ref: `"<name>@<version>"` of the stored stack that is used.
        name: Stored stack name.
        version: Stored stack version.
        hash: Identity hash (`"sha256:<hex>"`): the content hash with `stack.json`'s `name`,
            `version` and `description` left out.
        created_at: ISO-8601 time the entry was stored.
        manifest: Path of the manifest it was resolved from, or `None`.
        path: The stored stack directory (`<store>/<name>/<version>/config`).
        requested: The `name@version` that was asked for. It differs from `ref` when an identical
            stack already stored under another ref was reused.
    """

    ref: str
    name: str
    version: str
    hash: str
    created_at: str
    manifest: str | None
    path: Path
    requested: str

    @property
    def reused(self) -> bool:
        """True when an identical stack stored under another ref was returned instead of storing a new one."""
        return self.requested != self.ref
