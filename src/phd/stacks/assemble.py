"""Assemble a stack in today's config/ layout from a Manifest: base → agent → skills → mcp → hooks."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .._util.fs import write_json
from .._util.names import SAFE_NAME_RULE, is_safe_part
from ..errors import StackFetchError, StackInvalid
from .manifest import ComponentSource, Manifest
from .sources import Fetched, display_source, fetch

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git")
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
        _copytree(fetched.path, config, "base")
        meta = _read_json(config / "stack.json", "base stack.json")
        _validate_base_mcp(config / ".mcp.json")
        _validate_base_hooks(config / "hooks" / "hooks.json")
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
        provenance.append(_put_hooks(src, config, work, f"hooks[{i}]", bundles, base_entries))
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
    _check_name(md.name, "agent", "agent file")
    agents = config / "agents"
    if agents.is_dir():
        for old in agents.glob("*.md"):
            old.unlink()
    agents.mkdir(exist_ok=True)
    try:
        shutil.copy2(md, agents / md.name)
    except OSError as e:
        raise StackFetchError(f"agent: cannot copy the source into the stack ({type(e).__name__})") from None
    return _prov("agent", md.stem, src, fetched)


def _put_skill(src: ComponentSource, config: Path, work: Path, what: str) -> Provenance:
    fetched = fetch(src, work, what=what)
    _require(fetched, "SKILL.md", what)
    name = _frontmatter_name(fetched.path / "SKILL.md", what) or fetched.name
    _check_name(name, what, "skill")
    target = config / "skills" / name
    if target.exists():
        if not src.replace:
            raise StackInvalid(f"{what}: skill {name!r} already exists (base or an earlier entry); "
                               "set replace = true to replace it")
        shutil.rmtree(target)
    _copytree(fetched.path, target, what)
    return _prov("skill", name, src, fetched)


def _put_mcp(src: ComponentSource, config: Path, work: Path, what: str, servers: dict) -> Provenance:
    fetched = fetch(src, work, what=what)
    component = fetched.name
    if fetched.path.is_file():
        if not is_safe_part(component):  # e.g. ".mcp" from weather/.mcp.json: name it after the folder
            parent = _source_parent_name(src)
            component = parent if is_safe_part(parent) else "mcp"
        data, files = _read_json(fetched.path, what), None
    else:
        if not (fetched.path / ".mcp.json").is_file():
            raise StackFetchError(f"{what}: .mcp.json not found in {display_source(src.source)}")
        data, files = _read_json(fetched.path / ".mcp.json", what), fetched.path
    _check_name(component, what, "MCP component")
    entries = data.get("mcpServers")
    if not isinstance(entries, dict):
        raise StackInvalid(f"{what}: 'mcpServers' must be an object")
    if files is None and _mentions(entries, _HERE):
        raise StackInvalid(f"{what}: ${{here}} needs a folder source (a single JSON file has no folder in the stack)")
    if files is not None:
        target = config / "mcp" / component
        if target.exists():
            if not src.replace:
                raise StackInvalid(f"{what}: MCP component folder {component!r} already exists; set replace = true")
            # drop the old servers that point into the replaced folder, so none is left dangling
            prefix = f"${{stack}}/mcp/{component}"
            for k in [k for k, v in servers.items() if _references(v, prefix)]:
                del servers[k]
            shutil.rmtree(target)
        _copytree(files, target, what)
    here = f"${{stack}}/mcp/{component}"
    for key, server in entries.items():
        if key in servers and not src.replace:
            raise StackInvalid(f"{what}: MCP server {key!r} already exists; set replace = true to replace it")
        servers[key] = _rewrite(server, here)
    return _prov("mcp", component, src, fetched)


def _put_hooks(src: ComponentSource, config: Path, work: Path, what: str, bundles: dict,
               base_entries: list) -> Provenance:
    fetched = fetch(src, work, what=what)
    _require(fetched, "hooks.json", what)
    bundle = fetched.name
    _check_name(bundle, what, "hook bundle")
    _validate_hook_bundle_name(bundle, config / "hooks")
    target = config / "hooks" / bundle
    if target.exists() or bundle in bundles:
        if not src.replace:
            raise StackInvalid(f"{what}: hook bundle {bundle!r} already exists; set replace = true to replace it")
        if target.exists():
            shutil.rmtree(target)
        # the old bundle's entries go too: an earlier entry's (dict key below) and the base's
        base_entries[:] = [e for e in base_entries if not _references(e, f"${{stack}}/hooks/{bundle}")]
    entries = _read_json(fetched.path / "hooks.json", what).get("hooks")
    if not isinstance(entries, list):
        raise StackInvalid(f"{what}: 'hooks' must be an array")
    _copytree(fetched.path, target, what)
    bundles[bundle] = [_rewrite(e, f"${{stack}}/hooks/{bundle}") for e in entries]
    return _prov("hooks", bundle, src, fetched)


def _require(fetched: Fetched, marker: str, what: str) -> None:
    if not (fetched.path.is_dir() and (fetched.path / marker).is_file()):
        raise StackFetchError(f"{what}: {marker} not found in the source")


def _frontmatter_name(skill_md: Path, what: str) -> str | None:
    try:
        lines = skill_md.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        raise StackInvalid(f"{what}: SKILL.md is not UTF-8 text") from None
    except OSError as e:
        raise StackFetchError(f"{what}: cannot read SKILL.md ({type(e).__name__})") from None
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        # Only accept keys with no leading whitespace (top-level keys)
        if line and line[0] not in (' ', '\t'):
            key, sep, value = line.partition(":")
            if sep and key == "name":
                return value.strip().strip("'\"") or None
    return None


def _check_name(name: str, what: str, kind: str) -> None:
    if not is_safe_part(name):
        raise StackInvalid(f"{what}: invalid {kind} name {name!r}: use {SAFE_NAME_RULE}")


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


def _validate_base_mcp(mcp_file: Path) -> None:
    if mcp_file.is_file():
        data = _read_json(mcp_file, "base .mcp.json")
        if "mcpServers" in data and not isinstance(data["mcpServers"], dict):
            raise StackInvalid("base: 'mcpServers' must be an object")


def _validate_base_hooks(hooks_file: Path) -> None:
    if hooks_file.is_file():
        data = _read_json(hooks_file, "base hooks.json")
        if "hooks" in data and not isinstance(data["hooks"], list):
            raise StackInvalid("base: 'hooks' must be an array")


def _validate_hook_bundle_name(bundle: str, hooks_dir: Path) -> None:
    """Check if bundle name conflicts with existing files in hooks directory."""
    if bundle == "hooks.json":
        raise StackInvalid(f"hook bundle {bundle!r} conflicts with a hooks file")
    if hooks_dir.exists():
        file_path = hooks_dir / bundle
        if file_path.exists() and not file_path.is_dir():
            raise StackInvalid(f"hook bundle {bundle!r} conflicts with a hooks file")


def _references(value, prefix: str) -> bool:
    """True if some string nested in `value` names the folder `prefix` itself or a path inside it:
    `prefix` not followed by another name character, so `${stack}/mcp/git` does not match `${stack}/mcp/github`."""
    pattern = re.compile(re.escape(prefix) + r"(?![A-Za-z0-9._-])")
    return _mentions(value, pattern)


def _mentions(value, needle: str | re.Pattern) -> bool:
    """True if a string nested in `value` (dicts, lists) contains `needle` (a substring or a compiled regex)."""
    if isinstance(value, dict):
        return any(_mentions(v, needle) for v in value.values())
    if isinstance(value, list):
        return any(_mentions(v, needle) for v in value)
    if isinstance(value, str):
        return needle in value if isinstance(needle, str) else needle.search(value) is not None
    return False


def _source_parent_name(src: ComponentSource) -> str:
    """The name of the folder holding a single-file source (from the URL path for a URL)."""
    if src.is_url:
        return PurePosixPath(urlsplit(src.source).path.replace("\\", "/")).parent.name
    return Path(src.source).parent.name


def _copytree(source: Path, target: Path, what: str) -> None:
    try:
        shutil.copytree(source, target, ignore=_IGNORE)
    except OSError as e:  # shutil.Error is an OSError; its message lists paths, so only the type is shown
        raise StackFetchError(f"{what}: cannot copy the source into the stack ({type(e).__name__})") from None
