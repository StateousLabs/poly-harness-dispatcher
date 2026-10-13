"""Compose, resolve and store agent stacks from local paths and https URLs.

```python
import phd.stacks as stacks
stack = stacks.resolve("agents/reviewer/stack.toml")      # → Stack for Spec(stack=...)
stack = stacks.get("reviewer@1.3.0")                      # from the store, no fetching
```
"""

from __future__ import annotations

import builtins
import dataclasses
from pathlib import Path

from .._util.fs import robust_rmtree, write_json
from ..errors import StackInvalid
from ..spec import Stack, StoreEntry
from .assemble import assemble
from .manifest import ComponentSource, Manifest, compose, load_manifest
from .sources import display_source
from .store import StackStore, identity_hash


def resolve(source: str | Path | Manifest, *, allow_duplicates: bool = False, replace: bool = False,
            store_root: str | Path | None = None) -> Stack:
    """Fetch, assemble and store a stack; return it (loaded from the store, `stack.store` set).

    Args:
        source: A `stack.toml`, a folder containing one, a plain `config/` folder, or a `Manifest`
            from `compose(...)`. A URL string is refused: pass it as `compose(name, version, base=url)`.
        allow_duplicates: Store a new entry even when an identical stack is stored under another ref.
        replace: Overwrite a stored `name@version` that has different content.
        store_root: Store directory (default `$PHD_STACKS_ROOT` or `~/.phd/stacks`).

    Raises:
        StackInvalid: Bad manifest (or a URL string as `source`), a name clash without `replace = true`,
            the assembled stack fails the `Stack.load` checks, or the store is busy or not writable.
        StackFetchError: A source cannot be read or downloaded, or an archive is unusable/unsafe.
        StackConflict: `name@version` is stored with different content and `replace` is False.
    """
    manifest = _as_manifest(source)
    store = StackStore(store_root)
    staging = store.new_staging()
    try:
        provenance = assemble(manifest, staging)
        robust_rmtree(staging / "work")
        Stack.load(staging / "config")
        write_json(staging / "stack.lock.json",
                   {"ref": manifest.ref, "manifest": str(manifest.path) if manifest.path else None,
                    "components": [p.as_dict() for p in provenance]})
        entry = store.put(staging, name=manifest.name, version=manifest.version,
                          ident=identity_hash(staging / "config"),
                          manifest=str(manifest.path) if manifest.path else None,
                          allow_duplicates=allow_duplicates, replace=replace)
    finally:
        if staging.exists():
            robust_rmtree(staging)
    return _load(entry)


def get(ref: str, *, store_root: str | Path | None = None) -> Stack:
    """A stored stack: `"name@version"`, or `"name"` for its latest version. Raises `StackNotFound`."""
    return _load(StackStore(store_root).get(ref))


def list(*, store_root: str | Path | None = None) -> builtins.list[StoreEntry]:  # noqa: A001
    """Every stored stack, sorted by name then storage time."""
    return StackStore(store_root).list()


def remove(*refs: str, store_root: str | Path | None = None) -> None:
    """Remove stored stacks (`name@version`); nothing is removed if any ref is unknown."""
    StackStore(store_root).remove(*refs)


def prune(*, store_root: str | Path | None = None) -> None:
    """Delete leftover staging folders (older than 1 hour, and not owned by a running resolve) and index entries whose folder is gone."""
    StackStore(store_root).prune()


def _as_manifest(source: str | Path | Manifest) -> Manifest:
    if isinstance(source, Manifest):
        return source
    if isinstance(source, str) and "://" in source:  # never read a URL as a path (its query would leak)
        raise StackInvalid(f"{display_source(source.strip())}: pass a URL as compose(name, version, base=url)")
    path = Path(source).expanduser().resolve()
    if path.is_file():
        return load_manifest(path)
    if (path / "stack.toml").is_file():
        return load_manifest(path / "stack.toml")
    if (path / "stack.json").is_file():
        plain = Stack.load(path)
        return Manifest(name=plain.name, version=plain.version, base=ComponentSource(str(path)))
    raise StackInvalid(f"{path}: not a stack.toml, a folder with stack.toml, or a config/ folder with stack.json")


def _load(entry: StoreEntry) -> Stack:
    return dataclasses.replace(Stack.load(entry.path), store=entry)


__all__ = ["ComponentSource", "Manifest", "StoreEntry", "compose", "get", "list", "load_manifest", "prune",
           "remove", "resolve"]
