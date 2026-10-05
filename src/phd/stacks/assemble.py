"""Assemble a stack in today's config/ layout from a Manifest: base → agent → skills → mcp → hooks."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .._util.fs import write_json
from ..errors import StackFetchError, StackInvalid
from .manifest import ComponentSource, Manifest
from .sources import Fetched, display_source, fetch

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git")
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_HERE = "${here}"


@dataclass(frozen=True)
class Provenance:
    """Where one component of an assembled stack came from (written to stack.lock.json)."""

    kind: str
    name: str
    source: str
    digest: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, "source": self.source, "digest": self.digest}


def assemble(manifest: Manifest, staging: Path) -> list[Provenance]:
    """Write the assembled stack to `staging/config` (fetching into `staging/work`); return provenance."""
    config, work = staging / "config", staging / "work"
    work.mkdir(parents=True, exist_ok=True)
    provenance: list[Provenance] = []
    meta: dict = {}

    if manifest.base is not None:
        fetched = fetch(manifest.base, work, what="base")
        _require(fetched, "stack.json", "base")
        shutil.copytree(fetched.path, config, ignore=_IGNORE)
        meta = _read_json(config / "stack.json", "base stack.json")
        _rewrite_file(config / ".mcp.json", "${stack}")
        _rewrite_file(config / "hooks" / "hooks.json", "${stack}")
        provenance.append(_prov("base", meta.get("name", fetched.name), manifest.base, fetched))
    else:
        config.mkdir(parents=True)

    if manifest.agent is not None:
        provenance.append(_put_agent(manifest.agent, config, work))
    for i, src in enumerate(manifest.skills):
        provenance.append(_put_skill(src, config, work, f"skills[{i}]"))

    mcp_file = config / ".mcp.json"
    servers = _read_json(mcp_file, ".mcp.json").get("mcpServers", {}) if mcp_file.is_file() else {}
    for i, src in enumerate(manifest.mcp):
        provenance.append(_put_mcp(src, config, work, f"mcp[{i}]", servers))
    if servers or mcp_file.is_file():
        write_json(mcp_file, {"mcpServers": servers})

    hooks_dir = config / "hooks"
    base_entries: list = []
    if manifest.base_hooks:
        if (hooks_dir / "hooks.json").is_file():
            base_entries = _read_json(hooks_dir / "hooks.json", "hooks.json").get("hooks", [])
    elif hooks_dir.exists():
        shutil.rmtree(hooks_dir)
    bundles: dict[str, list] = {}
    for i, src in enumerate(manifest.hooks):
        provenance.append(_put_hooks(src, config, work, f"hooks[{i}]", bundles))
    if bundles or (manifest.base_hooks and base_entries):
        write_json(hooks_dir / "hooks.json", {"hooks": base_entries + [e for es in bundles.values() for e in es]})

    meta.update(name=manifest.name, version=manifest.version)
    if manifest.description is not None:
        meta["description"] = manifest.description
    if manifest.harnesses is not None:
        meta["harnesses"] = list(manifest.harnesses)
    if manifest.default_models is not None:
        meta["default_models"] = dict(manifest.default_models)
    write_json(config / "stack.json", meta)
    return provenance


def _put_agent(src: ComponentSource, config: Path, work: Path) -> Provenance:
    fetched = fetch(src, work, what="agent")
    if fetched.path.is_file():
        md = fetched.path
        if md.suffix.lower() != ".md":
            raise StackInvalid(f"agent: {display_source(src.source)} is not a .md file")
    else:
        mds = sorted(fetched.path.glob("*.md"))
        if len(mds) != 1:
            raise StackInvalid(f"agent: {display_source(src.source)} must contain exactly one .md file, found {len(mds)}")
        md = mds[0]
    agents = config / "agents"
    if agents.is_dir():
        for old in agents.glob("*.md"):
            old.unlink()
    agents.mkdir(exist_ok=True)
    shutil.copy2(md, agents / md.name)
    return _prov("agent", md.stem, src, fetched)


def _put_skill(src: ComponentSource, config: Path, work: Path, what: str) -> Provenance:
    fetched = fetch(src, work, what=what)
    _require(fetched, "SKILL.md", what)
    name = _frontmatter_name(fetched.path / "SKILL.md") or fetched.name
    _check_name(name, what, "skill")
    target = config / "skills" / name
    if target.exists():
        if not src.replace:
            raise StackInvalid(f"{what}: skill {name!r} already exists (base or an earlier entry); "
                               "set replace = true to replace it")
        shutil.rmtree(target)
    shutil.copytree(fetched.path, target, ignore=_IGNORE)
    return _prov("skill", name, src, fetched)


def _put_mcp(src: ComponentSource, config: Path, work: Path, what: str, servers: dict) -> Provenance:
    fetched = fetch(src, work, what=what)
    component = fetched.name
    _check_name(component, what, "MCP component")
    if fetched.path.is_file():
        data, files = _read_json(fetched.path, what), None
    else:
        if not (fetched.path / ".mcp.json").is_file():
            raise StackFetchError(f"{what}: .mcp.json not found in {display_source(src.source)}")
        data, files = _read_json(fetched.path / ".mcp.json", what), fetched.path
    entries = data.get("mcpServers")
    if not isinstance(entries, dict):
        raise StackInvalid(f"{what}: 'mcpServers' must be an object")
    if files is not None:
        target = config / "mcp" / component
        if target.exists():
            if not src.replace:
                raise StackInvalid(f"{what}: MCP component folder {component!r} already exists; set replace = true")
            shutil.rmtree(target)
        shutil.copytree(files, target, ignore=_IGNORE)
    here = f"${{stack}}/mcp/{component}"
    for key, server in entries.items():
        if key in servers and not src.replace:
            raise StackInvalid(f"{what}: MCP server {key!r} already exists; set replace = true to replace it")
        servers[key] = _rewrite(server, here)
    return _prov("mcp", component, src, fetched)


def _put_hooks(src: ComponentSource, config: Path, work: Path, what: str, bundles: dict) -> Provenance:
    fetched = fetch(src, work, what=what)
    _require(fetched, "hooks.json", what)
    bundle = fetched.name
    _check_name(bundle, what, "hook bundle")
    target = config / "hooks" / bundle
    if target.exists() or bundle in bundles:
        if not src.replace:
            raise StackInvalid(f"{what}: hook bundle {bundle!r} already exists; set replace = true to replace it")
        if target.exists():
            shutil.rmtree(target)
    shutil.copytree(fetched.path, target, ignore=_IGNORE)
    entries = _read_json(fetched.path / "hooks.json", what).get("hooks")
    if not isinstance(entries, list):
        raise StackInvalid(f"{what}: 'hooks' must be an array")
    bundles[bundle] = [_rewrite(e, f"${{stack}}/hooks/{bundle}") for e in entries]
    return _prov("hooks", bundle, src, fetched)


def _require(fetched: Fetched, marker: str, what: str) -> None:
    if not (fetched.path.is_dir() and (fetched.path / marker).is_file()):
        raise StackFetchError(f"{what}: {marker} not found in the source")


def _frontmatter_name(skill_md: Path) -> str | None:
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name":
            return value.strip().strip("'\"") or None
    return None


def _check_name(name: str, what: str, kind: str) -> None:
    if not _SAFE_NAME.fullmatch(name):
        raise StackInvalid(f"{what}: {kind} name {name!r} must match [A-Za-z0-9][A-Za-z0-9._-]*")


def _rewrite(value, here: str):
    if isinstance(value, dict):
        return {k: _rewrite(v, here) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite(v, here) for v in value]
    return value.replace(_HERE, here) if isinstance(value, str) else value


def _rewrite_file(path: Path, here: str) -> None:
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        if _HERE in text:
            write_json(path, _rewrite(json.loads(text), here))


def _read_json(path: Path, what: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise StackInvalid(f"{what}: {path.name} is not valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise StackInvalid(f"{what}: {path.name} must be a JSON object")
    return data


def _prov(kind: str, name: str, src: ComponentSource, fetched: Fetched) -> Provenance:
    return Provenance(kind=kind, name=name, source=display_source(src.source), digest=fetched.digest)
