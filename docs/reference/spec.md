# Spec and stack

This page is the API reference for `Spec`, `Inputs`, `Credentials`, `Stack` and `HookDef`. Field-by-field
behaviour per harness: [Spec, Inputs, Credentials](../concepts/spec-and-inputs.md) and [Stacks](../concepts/stacks.md).

## Summary

### Spec

`Spec(harness, model, stack, cwd, credentials, harness_config={}, project_config="ignore", autonomy="restricted", unsafe=False, secrets={}, timeout_s=1800.0, budget_usd=None, output_schema=None, resumable=True, harness_builtins=False, idle_timeout_s=None, schema_repair=False)`
Mutable dataclass describing what runs, where and how; validates everything in its constructor.

### Inputs

`Inputs(brief=None, msg=None, rules=None)`
Frozen dataclass: the task for one run. Resolves `brief` / `rules` to absolute paths; needs a brief or a non-blank msg.

### Credentials

| Member | Purpose |
|---|---|
| `Credentials.claude_oauth_token(token) -> Credentials` | claude, token from `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` |
| `Credentials.anthropic_api_key(key) -> Credentials` | claude, API key → `ANTHROPIC_API_KEY` |
| `Credentials.claude_host_login(path=None) -> Credentials` | claude, host login's access token only (default `~/.claude/.credentials.json`) |
| `Credentials.codex_auth_file(path=None) -> Credentials` | codex, `auth.json` copied into the run home (default `~/.codex/auth.json`) |
| `Credentials.opencode_auth_file(path=None, providers=()) -> Credentials` | opencode, auth JSON (filtered) → `OPENCODE_AUTH_CONTENT` |
| `Credentials.from_host(harness) -> Credentials` | the host login for any harness at its default path |
| `.claude_access_token() -> str` | read `claudeAiOauth.accessToken` from `path` |
| `.opencode_auth_content() -> str` | the opencode auth file as JSON text, filtered to `providers` |
| fields `harness`, `kind`, `secret`, `path`, `providers` | frozen; `secret` hidden from `repr()` |

### Stack

| Member | Purpose |
|---|---|
| `Stack.load(root) -> Stack` | load and validate a stack directory |
| `.ref -> str` | `"<name>@<version>"` |
| `.check_harness(harness) -> None` | raise `StackIncompatible` if `harnesses` excludes it |
| `.content_hash() -> str` | `"sha256:<hex>"` over the stack's files |
| fields `root`, `name`, `version`, `description`, `harnesses`, `default_models`, `agent_name`, `identity`, `skills`, `mcp_servers`, `hooks` | frozen dataclass |

### HookDef

`HookDef(event, command, tool=None, timeout_s=30.0)`, `.as_dict() -> dict`
Frozen dataclass: one neutral hook.

## Full reference

Rendered from the docstrings (MkDocs site only).

::: phd.Spec

::: phd.Inputs

::: phd.Credentials

::: phd.Stack

::: phd.HookDef

---

**Next:** [Dispatcher](dispatcher.md) · **See also:** [API overview](index.md), [Stacks](../concepts/stacks.md)
