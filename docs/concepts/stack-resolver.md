# Stack resolver

This page describes `phd.stacks`: how to compose a stack from several sources (local paths and URLs), how the
resolver assembles and stores it, how identical stacks are deduplicated, and every rule and error on the way. The
result of resolving is an ordinary [`Stack`](stacks.md), so `Spec(stack=...)` and the rest of PHD are unchanged.

```python
import phd.stacks as stacks
from phd import Spec

stack = stacks.resolve("agents/reviewer/stack.toml")   # fetch, assemble, store; returns a Stack
spec = Spec(harness="claude", model="sonnet", stack=stack, cwd="repo", credentials=...)
```

## Overview

```mermaid
flowchart LR
    M[stack.toml<br/>or compose] --> R[resolve]
    R --> F[fetch sources<br/>local / https]
    F --> A[assemble<br/>in .staging]
    A --> V[Stack.load checks]
    V --> H[identity hash]
    H --> S[(store<br/>name/version)]
    S --> K[Stack<br/>stack.store set]
```

Non-goals (by design):

- **No content verification.** No hash pins, signatures or scanning. What a source contains is the user's
  responsibility. The only refusals are infrastructure safety ones: an archive entry that would unpack outside its
  own folder, and the credential and redirect rules below.
- **Only URL consumption.** PHD reads `https://` URLs and local paths. There are no S3/GCS/git schemes (a pre-signed
  link is just an HTTPS URL), no download cache, no registry or server, no CLI.

## The manifest

A manifest is a `stack.toml` file, or the `Manifest` returned by `phd.stacks.compose(...)`. Both go through the same
parser and the same rules.

```toml
name = "reviewer"
version = "1.3.0"
description = "Reviews PRs"
harnesses = ["claude", "codex"]
default_models = { claude = "sonnet" }

base  = "https://stacks.example.com/reviewer-1.2.0.tar.gz"
agent = "./agents/AGENTS.md"
base_hooks = true

[[skills]]
source = "~/.phd/skills/pytest-helpers"

[[skills]]
source  = "https://example.com/pdf-2.1.zip"
replace = true

[[skills]]
source  = "https://private.example.com/x.tar.gz"
headers = { Authorization = "Bearer ${env:STACKS_TOKEN}" }

[[mcp]]
source = "./mcp/github"

[[hooks]]
source = "./hooks/block-rm-rf"
```

The same in code:

```python
m = stacks.compose(
    "reviewer", "1.3.0", description="Reviews PRs", harnesses=["claude", "codex"],
    base="https://stacks.example.com/reviewer-1.2.0.tar.gz", agent="agents/AGENTS.md",
    skills=["~/.phd/skills/pytest-helpers",
            {"source": "https://private.example.com/x.tar.gz",
             "headers": {"Authorization": "Bearer ${env:STACKS_TOKEN}"}}],
    mcp=["mcp/github"], hooks=["hooks/block-rm-rf"],
    root="agents/reviewer",            # relative sources resolve here (default: the current directory)
)
stack = stacks.resolve(m)
```

### Manifest keys

| Key | Type | Default | Validation |
|---|---|---|---|
| `name` | string | required | must match `[A-Za-z0-9][A-Za-z0-9._-]*` |
| `version` | string | required | same pattern |
| `description` | string | none | must be a string |
| `harnesses` | list of strings | none | non-empty list drawn from `claude`, `codex`, `opencode` |
| `default_models` | table of strings | none | must be a table whose values are strings |
| `base` | source string | none | a [source](#sources) |
| `agent` | source string | none | a source |
| `base_hooks` | bool | `true` | must be `true` or `false` |
| `skills` | array of tables `[[skills]]` | `[]` | each entry: see below |
| `mcp` | array of tables `[[mcp]]` | `[]` | same |
| `hooks` | array of tables `[[hooks]]` | `[]` | same |

Entry keys (`skills`, `mcp`, `hooks`):

| Key | Type | Default | Validation |
|---|---|---|---|
| `source` | string | required | a source |
| `replace` | bool | `false` | must be `true` or `false` |
| `headers` | table of strings | `{}` | keys and values must be strings; values may contain `${env:NAME}` |

An unknown top-level key or entry key, an entry that is not a table, an unreadable or invalid `stack.toml`: all
raise `StackInvalid` naming the file (or `compose()`) and the key.

### Sources

A source is either a **local path** or a **URL**.

| Form | Example | Notes |
|---|---|---|
| absolute path | `C:/stacks/base` | used as is |
| `~` path | `~/.phd/skills/pytest-helpers` | expanded |
| relative path | `./mcp/github` | resolved against the manifest's folder; for `compose`, against `root=` (default: the current directory) |
| `https://` URL | `https://example.com/x.tar.gz` | downloaded on every resolve |
| `http://localhost…`, `http://127.0.0.1…` | `http://127.0.0.1:8000/x.zip` | for local stack servers and tests only |

Any other scheme, or `http://` to another host, raises `StackInvalid`. Relative paths are resolved when the
manifest is parsed, so a `Manifest` is portable only as far as its absolute paths are.

A local path may be a folder, a single file, or an archive file. A URL is downloaded with the standard library
(timeout 60 s), then treated like a local file. URLs are fetched fresh on every `resolve` (URLs are mutable; the
dedupe step below stops identical results piling up).

### What each entry must contain

| Entry | Source content | Clash name |
|---|---|---|
| `base` | a whole stack in the [`config/` layout](stacks.md#layout) (folder or archive) with a `stack.json` | none |
| `agent` | exactly one `.md` file: the file itself, or a folder or archive containing exactly one `.md` | none: it always replaces the base's agent file(s) |
| `skills` | a folder with `SKILL.md` (plus `references/`, `scripts/`, anything else) | the `name:` in the `SKILL.md` front matter, else the folder name |
| `mcp` | a folder with `.mcp.json`, or a single JSON file `{"mcpServers": {...}}` | each server key; a folder's own name is the component folder |
| `hooks` | a folder with `hooks.json` plus the files it uses | the bundle (folder) name |

A skill, MCP component or hook bundle name must match `[A-Za-z0-9][A-Za-z0-9._-]*`. A folder with a missing marker
file raises `StackFetchError`; a malformed one raises `StackInvalid`.

### Archives

`.tar.gz`, `.tgz` and `.zip`. An archive is recognised by its file name (the `.zip` match is case-insensitive, so
`X.ZIP` works) or, when the name does not say, by sniffing the content (a URL such as `.../download` can serve an
archive). Content may sit at the archive root or inside **one** top-level folder; with a single top-level folder
that folder is the component (its name is the natural component name), otherwise the archive's file name without
the suffix is.

An entry that would unpack outside its folder (an absolute path, `..`, a link pointing out) is refused with
`StackFetchError`. This protects PHD's folders, not the content.

### `${here}`

In a component's `.mcp.json` and `hooks.json` (and in the base's), `${here}` means "this component's own folder in
the assembled stack". The resolver rewrites it to `${stack}/mcp/<component>` or `${stack}/hooks/<bundle>`, so a
stored stack only contains `${stack}` and the materializer is unchanged. In a base stack's files `${here}` becomes
`${stack}`.

```json
{ "mcpServers": { "github": { "command": "python", "args": ["${here}/server.py"] } } }
```

in a component folder `github/` becomes `"args": ["${stack}/mcp/github/server.py"]`. `${stack}` and `${env:NAME}`
keep their [usual meaning](stacks.md#placeholders).

## Assembly rules

The resolver builds `config/` in a staging folder in this order: **base, agent, skills, mcp, hooks**, then writes
`stack.json`.

| Step | What happens |
|---|---|
| base | copied as is (caches such as `__pycache__`, `*.pyc` and `.git` are skipped); its `.mcp.json` and `hooks/hooks.json` are checked |
| agent | the existing `agents/*.md` are deleted, the new file is copied in with its own file name |
| each skill | copied to `skills/<name>/` |
| each MCP | a folder component is copied to `mcp/<component>/` (a single JSON file has no files to copy); its servers are merged into `.mcp.json` |
| each hooks bundle | copied to `hooks/<bundle>/`; its entries are appended to `hooks/hooks.json` |
| `stack.json` | the base's `stack.json`, overridden by the manifest's `name`, `version` and (when given) `description`, `harnesses`, `default_models` |

### Metadata inheritance from the base

`stack.json` starts as the **base's** `stack.json`; the manifest's `name` and `version` always override it, and
`description`, `harnesses` and `default_models` override it only when the manifest sets them. A manifest that does
not set `harnesses` therefore inherits the base's list, and any other key in the base's `stack.json` is kept.

### Clash rules and `replace = true`

A clash is an error (`StackInvalid`) unless the entry has `replace = true`:

| Clash | Without `replace` | With `replace = true` |
|---|---|---|
| skill name already present (base or an earlier entry) | `StackInvalid` | the old skill folder is removed first |
| MCP server key already present | `StackInvalid` | the server is replaced |
| MCP component folder `mcp/<component>/` already present | `StackInvalid` | the folder is replaced, **and every earlier server whose config points into that folder (`${stack}/mcp/<component>`) is dropped** so none is left dangling |
| hook bundle name already present (including one from an earlier entry) | `StackInvalid` | the bundle is replaced |

The agent never clashes: `agent = ...` always replaces the base's agent.

A hook bundle named `hooks.json`, or one whose name collides with a hooks **file** (not a folder) in `hooks/`, is
**always** refused, even with `replace = true`, because it would shadow the combined hooks file.

### `base_hooks`

`base_hooks = true` (default) keeps the base's hook entries and appends the bundles' entries after them.
`base_hooks = false` deletes the base's whole `hooks/` folder first, so only the new bundles remain.

### `headers` and `${env:NAME}`

Headers apply to the one source they sit on. `${env:NAME}` in a header value is expanded from the **process
environment** (not `Spec.secrets`) at resolve time:

```toml
[[skills]]
source  = "https://private.example.com/x.tar.gz"
headers = { Authorization = "Bearer ${env:STACKS_TOKEN}" }
```

- A missing variable raises `StackInvalid` naming the header and the variable.
- A header value that expands to something containing a carriage return, line feed or NUL is refused with
  `StackInvalid` (it would allow header injection); the message names the header and the variable, never the value.
- Headers are used only for that download: they are never written to the store, `index.json` or `stack.lock.json`.

### Credentials never appear in errors or files

- Error messages, `stack.lock.json` and `index.json` show a URL **without its query string, fragment or userinfo**
  (`https://host/path/x.tar.gz`), so pre-signed links and `user:password@` URLs do not leak.
- Header values never appear in an error. Low-level request errors are reduced to their type name
  (for example `request failed (TimeoutError)`).

### Redirects

URL downloads follow redirects with these rules:

| Redirect | Behaviour |
|---|---|
| same origin (scheme, host and port equal) | followed; your `headers` are kept |
| different origin | followed; your `headers` are **dropped** (a token for one host is not sent to another) |
| `https` to `http` | refused with `StackFetchError`, unless the target is `localhost` or `127.0.0.1` |

The component name comes from the URL **you gave**, not from the redirect target.

## Resolution steps

`resolve(source)` does, in order:

1. **Read and validate** the manifest (`StackInvalid`). `source` may be a `stack.toml`, a folder containing one, a
   plain `config/` folder (its own `stack.json` supplies name and version and it becomes the `base`), or a
   `Manifest`.
2. **Fetch** every source into a work area under the staging folder (`StackFetchError`).
3. **Assemble** into `<store>/.staging/<uuid>/config/` as above.
4. **Validate** with the same checks as `Stack.load`: exactly one agent file, a `SKILL.md` per skill, valid MCP
   entries and hooks (`StackInvalid`, `HookUnsupported`).
5. **Hash**: the [content hash](stacks.md#content_hash) of the assembled `config/` with `stack.json`'s `name`,
   `version` and `description` removed. `harnesses` and `default_models` do count.
6. **Provenance**: write `stack.lock.json` next to `config/`: the manifest ref and path, and for every component its
   `kind`, `name`, `source` (path or URL without query or headers) and `digest` (sha256 of the fetched bytes or
   tree).
7. **Store** the staged folder (see below), then delete the staging folder, and return the `Stack` loaded from the
   stored `config/` with `stack.store` set.

## The store

Default root `~/.phd/stacks`; override per call with `store_root=` or globally with the `PHD_STACKS_ROOT`
environment variable (the argument wins).

```
<store>/
  index.json                        {"reviewer@1.3.0": {"hash": "sha256:...", "created_at": "...", "manifest": "<path or null>"}}
  reviewer/1.3.0/config/            the assembled stack (config/ layout)
  reviewer/1.3.0/stack.lock.json    provenance
  .staging/<uuid>/                  in-progress resolves
  .store.lock                       the store's lock file
```

### Names

`name` and `version` become folder names, so they must match `[A-Za-z0-9][A-Za-z0-9._-]*`, must **not end in
`.`**, and must not be a Windows device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1` to `COM9`, `LPT1` to `LPT9`, with
or without an extension, any case). Otherwise `StackInvalid`. If the target folder already exists but is **not in
the index** (for example a case-only collision on Windows, where `Reviewer` and `reviewer` are the same folder),
`StackInvalid` is raised instead of overwriting it.

### Dedupe: the four cases

Storing a staged stack with ref `name@version` and identity hash `h`:

| State | `allow_duplicates=False` (default) | `allow_duplicates=True` |
|---|---|---|
| no entry with this ref, no entry with hash `h` | store under the ref | store under the ref |
| this ref exists with hash `h` | reuse it (nothing changes) | reuse it |
| a different ref has hash `h` | **reuse that one** (the oldest such entry) | store under the ref |
| this ref exists with a different hash | `StackConflict`, unless `replace=True` (overwrite) | same |

When an identical stack stored under another ref is reused, nothing new is written and the returned stack is that
other entry:

```python
a = stacks.resolve(m1)                       # reviewer@1.0.0 stored
b = stacks.resolve(m2)                       # reviewer@1.0.1, identical content
b.store.ref        # "reviewer@1.0.0"        the stack that is actually used
b.store.requested  # "reviewer@1.0.1"        what you asked for
b.store.reused     # True
```

`StoreEntry.reused` is `requested != ref`. A new version with different content is stored next to the old one.

### Conflicts and `replace=True`

```python
stacks.resolve(m)                    # reviewer@1.3.0 stored
stacks.resolve(m_changed)            # same name@version, other content: raises StackConflict
stacks.resolve(m_changed, replace=True)   # overwrites reviewer@1.3.0
```

A replace moves the new folder in first and updates the index before deleting the old folder; if the move fails the
old stack is put back.

### Locking and the index

Every write (`resolve`'s store step, `remove`, `prune`) runs under an OS file lock on `<store>/.store.lock`, so
concurrent processes serialize and leave a valid `index.json`. The lock is released by the operating system if its
process dies, so there is no stale lock to clean up. A corrupt or unreadable `index.json` raises `StackInvalid`
naming the file (fix or delete it; `prune` cannot repair it).

### `get`, `list`, `remove`, `prune`

| Call | Behaviour |
|---|---|
| `stacks.get("reviewer@1.3.0")` | the exact stack, loaded from the store without fetching; `StackNotFound` if unknown |
| `stacks.get("reviewer")` | the entry of that name with the most recent `created_at` |
| `stacks.list()` | every `StoreEntry`, sorted by name then storage time |
| `stacks.remove("a@1", "b@2")` | removes those entries and their folders; if **any** ref is unknown, `StackNotFound` is raised and nothing is removed |
| `stacks.prune()` | deletes `.staging/` leftovers older than 1 hour and index entries whose folder is gone |

All of them take `store_root=`.

### What removal means for runs

A run copies its stack into its own run home when it is planned, so removing a stack never changes a run that is
already going or finished. A later `plan`, `start`, `spawn` or resume whose `Spec.stack.root` no longer exists fails
**before launch** with `StackNotFound`. Re-resolve the stack (or `stacks.get` another one) and build a new `Spec`.

---

**Next:** [Spec, Inputs, Credentials](spec-and-inputs.md) · **See also:** [Stacks](stacks.md), [API: stacks](../reference/stacks.md), [Troubleshooting](../troubleshooting.md)
