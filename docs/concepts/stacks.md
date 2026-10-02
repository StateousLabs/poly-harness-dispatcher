# Stacks

This page describes the stack directory (`config/` layout): every file PHD reads, how each part becomes harness
configuration, the validation errors, and how `content_hash` is computed.

A stack is a **harness-neutral agent**: one identity plus skills, MCP servers and hooks. `Stack.load(path)`
reads and validates it once; every run then copies the directory into its own run home and builds the
harness-specific configuration from it. The stack itself is never modified, so many runs can share one stack.

## Layout

```
config/                      any directory name
  stack.json                 required: name, version (+ optional fields)
  agents/<one>.md            required: exactly one .md file = the agent's identity
  skills/<name>/SKILL.md     optional: one folder per skill (+ references/, scripts/, anything else)
  .mcp.json                  optional: MCP servers (neutral format = Claude's .mcp.json schema)
  hooks/hooks.json           optional: neutral hooks; hook scripts usually live next to it in hooks/
  ...                        anything else (MCP server code, data); copied with the stack
```

Only a directory is accepted (`Stack.load` does not read tarballs; see
[Architecture: planned stack resolver](../architecture.md#roadmap-components)).

## `stack.json`

| Field | Type | Required | Validation | Used for |
|---|---|---|---|---|
| `name` | string | yes | non-empty | `Stack.ref` = `name@version`, recorded as `RunResult.stack` |
| `version` | string | yes | non-empty | same |
| `description` | string | no (default `""`) | none | `Stack.description` only |
| `harnesses` | list of strings | no (default: any harness) | non-empty list drawn from `claude`, `codex`, `opencode` | `Spec` raises `StackIncompatible` if `Spec.harness` is not listed |
| `default_models` | object of strings | no (default `{}`) | must be an object; values must be strings | stored as `Stack.default_models`; **PHD never reads it** (`Spec.model` is always required) |

Other keys are ignored.

```json
{
  "name": "fixture-helper",
  "version": "0.1.0",
  "description": "Test stack: one agent, two skills, one MCP, one hook.",
  "harnesses": ["claude", "codex", "opencode"],
  "default_models": { "claude": "haiku" }
}
```

## `agents/<one>.md`: the identity

- `agents/` must contain **exactly one** `*.md` file; its name is free (`Stack.agent_name` = file stem).
- The whole file text is the identity (`Stack.identity`), read once at `Stack.load` time. Front matter is not
  parsed; it would be included verbatim.
- At run time the identity, stripped, becomes the first block of the system prompt, followed by the rules file and
  (for `project_config="instructions_only"`) the repo's instruction files. See
  [Prompt assembly](prompt-assembly.md).

How that system prompt reaches each harness:

| Harness | File in the run home | Mechanism |
|---|---|---|
| claude | `home/system-prompt.md` | `--append-system-prompt-file <file>` (appended to Claude Code's own system prompt) |
| codex | `home/AGENTS.md` | `CODEX_HOME=<home>`: codex reads `$CODEX_HOME/AGENTS.md` as global instructions |
| opencode | `home/AGENTS.md` | `"instructions": ["<home>/AGENTS.md"]` in `home/config/opencode.json` |

## Skills

- Every **directory** directly under `skills/` is a skill and must contain `SKILL.md`, else `StackInvalid`.
  Plain files directly in `skills/` are ignored. Skills are sorted by folder name.
- PHD does not parse `SKILL.md` (front matter is the harness's business); the folder is copied as a whole,
  including `references/`, `scripts/` and any other files.
- Copied at materialize time from the original stack directory to:

| Harness | Skills directory in the run home |
|---|---|
| claude | `home/skills/<name>/` (`CLAUDE_CONFIG_DIR/skills`) |
| codex | `home/skills/<name>/` (`CODEX_HOME/skills`) |
| opencode | `home/config/skills/<name>/` (`OPENCODE_CONFIG_DIR/skills`) |

By default the harness's own bundled skills are suppressed so the agent sees only the stack's skills
(`Spec.harness_builtins=False`; see [Harnesses](harnesses.md#built-in-skill-suppression-harness_builtins)).

## `.mcp.json`: MCP servers

The neutral format is Claude's `.mcp.json` schema. The file is optional; if present its top level must be an
object, and `mcpServers` (optional) must be an object of servers.

| Server `type` | Required key | Optional keys passed through | Notes |
|---|---|---|---|
| `"stdio"` (default when `type` is absent) | `command` (string) | `args` (list), `env` (object) | |
| `"http"` | `url` (string) | `headers` (object) | codex translation is unverified (PHD adds a warning) |
| anything else | | | `StackInvalid` |

```json
{
  "mcpServers": {
    "echo": {
      "type": "stdio",
      "command": "python",
      "args": ["${stack}/mcp/echo_server.py"],
      "env": { "ECHO_PREFIX": "${env:ECHO_PREFIX}" }
    }
  }
}
```

### Placeholders

Placeholders are resolved in every string value of every server (recursively through objects and lists) and in
every hook `command`:

| Placeholder | Resolves to | If missing |
|---|---|---|
| `${stack}` | The run's copy of the stack, `<run_dir>/home/stack`, as a forward-slash path. In MCP config it is inserted as-is; in hook commands it is shell-quoted when the path contains spaces or shell metacharacters (`&`, `<`, `>`, `^`, parentheses, the pipe character). | n/a |
| `${env:NAME}` | `Spec.secrets["NAME"]` (`NAME` matches `[A-Za-z_][A-Za-z0-9_]*`) | `Spec(...)` raises `MissingSecret` listing every missing name |

`${env:NAME}` is **not** read from the host environment; it only comes from `Spec.secrets`. Resolved values are
written into the harness config files in the run home (see
[Security](security-and-isolation.md#secret-handling)).

Per-harness translation of servers (claude `mcp.json`, codex `config.toml [mcp_servers]`, opencode `mcp`):
[Harnesses](harnesses.md).

## `hooks/hooks.json`: neutral hooks

Optional. Top level must be an object; `hooks` (optional) must be an array.

| Key | Type | Required | Validation |
|---|---|---|---|
| `event` | string | yes | one of `session_start`, `pre_tool`, `post_tool`, `stop`, else `HookUnsupported` |
| `command` | string | yes | else `StackInvalid`. A shell command line; `${stack}` and `${env:NAME}` allowed |
| `match` | object | no | must be an object if present; only `match.tool` is read (a neutral tool name) |
| `timeout_s` | number | no (default 30) | must convert to `float`, else `StackInvalid` |

```json
{
  "hooks": [
    { "event": "pre_tool", "match": { "tool": "shell" }, "command": "python ${stack}/hooks/guard.py", "timeout_s": 10 }
  ]
}
```

Whether an event/tool combination is supported depends on the harness and is checked when the `Dispatcher` is
created (`HookUnsupported`). Translation, payload and exit codes: [Hooks](hooks.md).

## Validation errors

All raised by `Stack.load` unless noted.

| Error | Cause |
|---|---|
| `StackInvalid` | `stack.json` missing, invalid JSON, not an object; `name` / `version` missing or blank; bad `harnesses`; bad `default_models` |
| `StackInvalid` | `agents/` missing or not exactly one `*.md` |
| `StackInvalid` | a `skills/<dir>` without `SKILL.md` |
| `StackInvalid` | `.mcp.json` invalid JSON / not an object; `mcpServers` not an object; a server not an object; stdio without `command`; http without `url`; unknown `type` |
| `StackInvalid` | `hooks/hooks.json` invalid JSON / not an object; `hooks` not an array; a hook without a `command` string; `match` not an object; non-numeric `timeout_s` |
| `HookUnsupported` | a hook `event` outside the neutral set |
| `StackIncompatible` | raised by `Spec(...)`: `harnesses` is set and excludes `Spec.harness` |
| `MissingSecret` | raised by `Spec(...)`: `${env:NAME}` used but `NAME` not in `Spec.secrets` |
| `HookUnsupported` | raised by `Dispatcher(...)`: event or tool not expressible for the harness |

Every message names the offending file.

## What is read when

| Part | Read at | Consequence of editing the stack afterwards |
|---|---|---|
| `stack.json`, identity text, `.mcp.json`, `hooks.json` | `Stack.load` | not picked up until you load again |
| the **list** of skill folders | `Stack.load` | a newly added or removed skill folder needs a new `Stack.load` |
| contents of the known skill folders, all other files | each `plan` / `start` (copied into the run home) | picked up by the next run |
| `content_hash()` | each `plan` / `start` | reflects the files at plan time |

## `content_hash`

`Stack.content_hash()` returns `"sha256:<hex>"` over the stack directory. The dispatcher calls it when a run is
planned and records it as `RunResult.stack_hash` (a resumed run keeps the original run's hash).

Rules:

- Files are visited in sorted order; for each file the hash takes its path relative to the stack root (forward
  slashes), a NUL byte, its raw bytes, and a NUL byte. No line-ending normalization: a CRLF checkout hashes
  differently from an LF one.
- Skipped: files under any directory whose name starts with `.` (relative to the stack root, so `.git/` is
  skipped, while the `.mcp.json` *file* and hidden files inside normal folders are hashed); files under
  `__pycache__/`; `*.pyc` files.
- The stack root's own location does not matter (a stack under `~/.phd/` hashes like anywhere else).

The run-home copy (`home/stack/`) is a full `copytree` and does include `.git/` and `__pycache__/`.

---

**Next:** [Spec, Inputs, Credentials](spec-and-inputs.md) · **See also:** [Hooks](hooks.md), [Harnesses](harnesses.md), [API: Stack](../reference/spec.md#stack)
