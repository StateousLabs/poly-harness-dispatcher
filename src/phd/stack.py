"""Harness-neutral agent stack (CoreDesign §2b): one identity plus skills, MCP servers and
neutral hooks, loaded from a config/ directory. A stack is read-only; every run gets its own
config home built from it."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .constants import HARNESSES, NEUTRAL_EVENTS
from .errors import HookUnsupported, StackIncompatible, StackInvalid


@dataclass(frozen=True)
class HookDef:
    event: str
    command: str
    tool: str | None = None
    timeout_s: float = 30.0

    def as_dict(self) -> dict:
        return {"event": self.event, "command": self.command, "tool": self.tool, "timeout_s": self.timeout_s}


@dataclass(frozen=True)
class Stack:
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

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    @classmethod
    def load(cls, root: str | Path) -> Stack:
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
        if self.harnesses is not None and harness not in self.harnesses:
            raise StackIncompatible(f"stack {self.ref} supports {list(self.harnesses)}, not {harness!r}")

    def content_hash(self) -> str:
        """Compute SHA256 hash of all stack files, skipping cache and hidden files.

        Excludes:
        - Any path component starting with "." (e.g., .git, .mcp.json is included since it's the filename)
        - Any path component named "__pycache__"
        - Any *.pyc files

        Uses raw bytes (no line-ending normalization).
        """
        digest = hashlib.sha256()
        for path in sorted(p for p in self.root.rglob("*") if p.is_file()):
            # Skip __pycache__ directories
            if "__pycache__" in path.parts:
                continue
            # Skip hidden directories (but allow hidden files like .mcp.json)
            if any(part.startswith(".") and part != "." for part in path.parts[:-1]):
                continue
            # Skip .pyc files
            if path.suffix == ".pyc":
                continue
            digest.update(path.relative_to(self.root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
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
