# Stacks (resolver)

This page is the API reference for `phd.stacks`: composing, resolving and storing stacks. How it behaves and why:
[Stack resolver](../concepts/stack-resolver.md). Import it as a module: `import phd.stacks as stacks`.

## Summary

### Functions

| Member | Purpose |
|---|---|
| `resolve(source, *, allow_duplicates=False, replace=False, store_root=None) -> Stack` | fetch, assemble and store a stack (`source`: a `stack.toml`, a folder with one, a `config/` folder, or a `Manifest`); returns it loaded from the store with `stack.store` set |
| `get(ref, *, store_root=None) -> Stack` | a stored stack by `"name@version"`, or `"name"` for its latest version; `StackNotFound` if unknown |
| `list(*, store_root=None) -> list[StoreEntry]` | every stored stack, sorted by name then storage time |
| `remove(*refs, store_root=None) -> None` | remove stored stacks; nothing is removed if any ref is unknown |
| `prune(*, store_root=None) -> None` | delete `.staging` leftovers older than 1 hour and index entries whose folder is gone |
| `compose(name, version, *, description=None, harnesses=None, default_models=None, base=None, agent=None, base_hooks=True, skills=(), mcp=(), hooks=(), root=None) -> Manifest` | build a `Manifest` in code (same rules as `stack.toml`) |
| `load_manifest(path) -> Manifest` | parse and validate a `stack.toml` |

### Types

| Member | Purpose |
|---|---|
| `Manifest(name, version, description=None, harnesses=None, default_models=None, base=None, agent=None, base_hooks=True, skills=(), mcp=(), hooks=(), path=None)` | frozen dataclass: a parsed manifest; `.ref` is `"name@version"` |
| `ComponentSource(source, replace=False, headers={})` | frozen dataclass: one source (path or URL) with its `replace` flag and headers; `.is_url` |
| `StoreEntry(ref, name, version, hash, created_at, manifest, path, requested)` | frozen dataclass: where a stack lives in the store; `.reused` is true when an identical stack under another ref was returned |

Errors: `StackInvalid`, `StackFetchError`, `StackConflict`, `StackNotFound` (see [Errors](errors.md)).

## Full reference

Rendered from the docstrings (MkDocs site only).

::: phd.stacks.resolve

::: phd.stacks.get

::: phd.stacks.list

::: phd.stacks.remove

::: phd.stacks.prune

::: phd.stacks.compose

::: phd.stacks.load_manifest

::: phd.stacks.Manifest

::: phd.stacks.ComponentSource

::: phd.stacks.StoreEntry

---

**Next:** [Dispatcher](dispatcher.md) · **See also:** [Stack resolver](../concepts/stack-resolver.md), [API overview](index.md)
