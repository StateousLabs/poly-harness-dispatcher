# Spec, Inputs, Credentials

This page lists every field of `Spec`, `Inputs` and `Credentials`: type, default, validation, and what it does for
each harness.

The split: **`Spec`** says what runs, where and how (one `Dispatcher` per spec); **`Inputs`** is the task for one
run; **`Credentials`** is how that run authenticates; the **`Stack`** ([Stacks](stacks.md)) is who the agent is.

## Spec

`Spec` is a mutable dataclass that validates everything in its constructor, in this order:
harness, model, cwd, project_config, autonomy, unsafe, credentials, harness_builtins, timeout_s, budget_usd,
idle_timeout_s, schema_repair, output_schema, stack compatibility, secrets.

| Field | Type | Default | Validation (exception) | Effect |
|---|---|---|---|---|
| `harness` | `str` | required | one of `claude`, `codex`, `opencode` (`UnknownHarness`) | selects the adapter |
| `model` | `str` | required | non-blank string (`InvalidSpec`); catalogue check in `Dispatcher` (`ModelNotSupported`) | claude `--model`, codex `-m` and `model` in `config.toml`, opencode `-m` (`provider/model`) |
| `stack` | `Stack` | required | `harnesses` list must include `harness` (`StackIncompatible`) | identity, skills, MCP, hooks |
| `cwd` | `Path` (str accepted) | required | expanded and resolved; must be a directory (`CwdNotFound`) | process working directory for every harness; also codex `-C <cwd>`, opencode `--dir <cwd>` |
| `credentials` | `Credentials` | required | `credentials.harness == harness` (`MissingCredentials`) | see [Credentials](#credentials) |
| `harness_config` | `dict` | `{}` | none | deep-merged into the harness config file (below) |
| `project_config` | `str` | `"ignore"` | `ignore`, `instructions_only`, `full` (`InvalidSpec`) | see [project_config](#project_config) |
| `autonomy` | `str` | `"restricted"` | `restricted`, `full` (`InvalidSpec`) | see [autonomy](#autonomy) |
| `unsafe` | `bool` | `False` | `autonomy="full"` requires `unsafe=True` (`UnsafeAutonomy`) | recorded as `RunResult.unsafe`; no other effect |
| `secrets` | `dict[str, str]` | `{}` | every `${env:NAME}` in the stack's `.mcp.json` and hook commands must have a key (`MissingSecret`) | substituted into MCP / hook config; scrubbed from the crash stderr tail; hidden from `repr()` |
| `timeout_s` | `float` | `1800.0` | `> 0` (`InvalidSpec`) | wall-clock limit from process start; on expiry the tree is killed, status `timed_out` |
| `budget_usd` | `float or None` | `None` | `> 0` when set (`InvalidSpec`) | claude `--max-budget-usd` + PHD check; opencode PHD check; codex warning only. See [combinations](combinations.md#budget-enforcement) |
| `idle_timeout_s` | `float or None` | `None` | a positive number when set (`InvalidSpec`) | kill the run when the harness prints no stdout line for this long: `idle_timeout`, status `timed_out`. Claude and codex are silent during tool calls: set it above your longest tool call. See [Run lifecycle](run-lifecycle.md#idle-timeout) |
| `output_schema` | `dict or None` | `None` | a dict that is a valid JSON Schema (`InvalidSpec`) | claude `--json-schema '<json>'`; codex `--output-schema home/output-schema.json`; opencode: an instruction in `AGENTS.md`. PHD validates the answer on every harness (error kind `schema`). See [combinations](combinations.md#output_schema) |
| `schema_repair` | `bool` | `False` | must be a real `bool` (`InvalidSpec`) | with `output_schema`: on a schema error, resume the session once and ask for a corrected answer. See [Run lifecycle](run-lifecycle.md#schema-repair-turn) |
| `resumable` | `bool` | `True` | none | `False`: the run home is deleted after the result is built, so the session cannot be resumed (a resumed turn never deletes the shared home) |
| `harness_builtins` | `bool` | `False` | must be a real `bool` (`InvalidSpec`) | `False` suppresses the harness's bundled skills/plugins; see [Harnesses](harnesses.md#built-in-skill-suppression-harness_builtins) |

### harness_config

`harness_config` is merged into the config file PHD writes for the harness:

| Harness | File |
|---|---|
| claude | `home/settings.json` |
| codex | `home/config.toml` |
| opencode | `home/config/opencode.json` |

Merge rules (PHD's generated config is the base, yours is merged in):

- keys PHD does not set are added;
- objects merge recursively;
- lists are unioned (PHD's items first, then yours that are not already present);
- a scalar that differs from PHD's value is **ignored**, PHD's value is kept, and a warning
  `harness_config key 'a.b' conflicts with a PHD-owned value; PHD's value was kept` is added to the run.

So you can add permissions (for example claude `{"permissions": {"allow": ["Bash(echo:*)"]}}` or opencode
`{"permission": {"bash": {"echo *": "allow"}}}`), but you cannot override PHD-owned scalars such as codex
`sandbox_mode` or claude `disableBundledSkills`.

### project_config

How much of the repository's own agent configuration (in `cwd`) the run sees.

| Mode | Repo `AGENTS.md` / `CLAUDE.md` | Repo settings, hooks, MCP, skills |
|---|---|---|
| `ignore` (default) | not loaded | not loaded |
| `instructions_only` | PHD reads `cwd/AGENTS.md` and `cwd/CLAUDE.md` (only in `cwd`, not parents) and appends them to the system prompt | not loaded |
| `full` | loaded natively by the harness | loaded natively by the harness (repo hooks are code PHD does not control) |

Per-harness mechanism:

| Harness | `ignore` and `instructions_only` | `full` |
|---|---|---|
| claude | `--setting-sources user` (the "user" level is the run home) | `--setting-sources user,project,local` |
| codex | `project_doc_max_bytes = 0` in `config.toml`; `--ignore-rules`; repo-level `.agents/skills/*/SKILL.md` from `cwd` up to the filesystem root disabled in `config.toml` | none of those |
| opencode | `OPENCODE_DISABLE_PROJECT_CONFIG=1` | variable not set |

### autonomy

| Harness | `restricted` (default) | `full` (needs `unsafe=True`) |
|---|---|---|
| claude | `--permission-mode acceptEdits` | `--dangerously-skip-permissions` |
| codex | `config.toml`: `approval_policy = "never"`, `sandbox_mode = "workspace-write"` | `approval_policy = "never"`, `sandbox_mode = "danger-full-access"` |
| opencode | `permission = {"edit": "allow", "bash": {"*": "deny"}, "external_directory": "deny"}` | `permission = {"*": "allow"}` and `--auto` |

`restricted` relies on the harness's own permission system and is **not a security boundary**; see
[Security and isolation](security-and-isolation.md). Under `restricted`, claude and opencode deny shell commands
unless you allow them through `harness_config` (the quickstart allows `echo`).

## Inputs

A frozen dataclass; validated in its constructor.

| Field | Type | Default | Validation | Becomes |
|---|---|---|---|---|
| `brief` | `Path or None` (str accepted) | `None` | expanded (`~`) and resolved to an absolute path; must be a file (`InputNotFound`) | `# Task brief` block of the first user message |
| `msg` | `str or None` | `None` | none on its own | `# Message` block, after the brief |
| `rules` | `Path or None` (str accepted) | `None` | as `brief` (`InputNotFound`) | `## Project rules (binding)` block of the system prompt; not allowed on a resumed turn (`UnsupportedFeature`) |

At least one of `brief` and a non-blank `msg` is required (`EmptyTask`). Relative paths resolve against the
**Python process's current directory**, not `Spec.cwd`. File contents are read when the run is materialized
(`plan` / `start`), not when `Inputs` is created. Exact assembly: [Prompt assembly](prompt-assembly.md).

```python
Inputs(brief="briefs/task-42.md", rules="RULES.md", msg="focus on the parser")
Inputs(msg="List your skills.")
Inputs(rules="RULES.md")          # EmptyTask: rules alone is not a task
```

## Credentials

A frozen dataclass; build it with a constructor. `secret` is excluded from `repr()`, and only `kind` is recorded
(`RunResult.credential_kind`).

| Constructor | `harness` | `kind` | Checked at construction | Read at materialize / every resume | Injected as |
|---|---|---|---|---|---|
| `Credentials.claude_oauth_token(token)` | claude | `claude_oauth_token` | nothing | (held in memory) | env `CLAUDE_CODE_OAUTH_TOKEN` |
| `Credentials.anthropic_api_key(key)` | claude | `anthropic_api_key` | nothing | (held in memory) | env `ANTHROPIC_API_KEY` |
| `Credentials.claude_host_login(path=None)` | claude | `claude_host_login` | file exists (default `~/.claude/.credentials.json`), else `MissingCredentials` | `claudeAiOauth.accessToken` from the file | env `CLAUDE_CODE_OAUTH_TOKEN` (access token only) |
| `Credentials.codex_auth_file(path=None)` | codex | `codex_auth_file` | file exists (default `~/.codex/auth.json`) | whole file | copied to `home/auth.json` (mode 0600 on POSIX) |
| `Credentials.opencode_auth_file(path=None, providers=())` | opencode | `opencode_auth_file` | file exists (default `~/.local/share/opencode/auth.json`) | JSON, filtered to `providers` (all if empty) | env `OPENCODE_AUTH_CONTENT` (no file written) |
| `Credentials.from_host(harness)` | any | as above | as above; unknown harness → `UnknownHarness` | | |

Read-time errors (`MissingCredentials`): unreadable or invalid JSON, top level not an object, no
`claudeAiOauth.accessToken`, or a requested opencode provider missing from the file.

Notes:

- **Refresh tokens.** `claude_host_login` never copies the refresh token, so a run cannot rotate (and so
  invalidate) the host login, but the access token expires; runs then fail with `error.kind == "auth"`. Prefer
  `claude_oauth_token` from `claude setup-token`. `codex_auth_file` copies the whole `auth.json`, including any
  refresh token; whether codex rotates it inside the run home (invalidating the host copy) is unverified
  (CoreDesign §7). `opencode_auth_file` passes whatever the selected provider entries contain.
- **Host variables are not used.** The child process gets the host environment minus every `ANTHROPIC_*`,
  `CLAUDE_*`, `CODEX_*`, `OPENAI_*`, `OPENCODE_*` and `XDG_*` variable, so an `ANTHROPIC_API_KEY` in your shell
  does not leak into a run. There is no `*_from_env()` constructor and no `openai_api_key` kind today.
- **macOS:** the claude login lives in the Keychain; use `claude_oauth_token`.

---

**Next:** [Harnesses](harnesses.md) · **See also:** [Option combinations](combinations.md), [API: Spec and stack](../reference/spec.md)
