# Harnesses

This page is the per-harness reference: the run-home layout, the exact command line and environment, how
credentials, identity, rules, skills, MCP servers and hooks are materialized, how `project_config`, `autonomy`
and `harness_builtins` map, how resume works, where cost, usage and the model catalogue come from, and the known
quirks.

Facts here come from the adapters (`src/phd/harnesses/<name>/adapter.py`, `parser.py`, `models.py`) and the
spike results in [CoreDesign §14](../CoreDesign.md). CLI versions the behaviour was verified against: claude
2.1.284 to 2.1.287, codex 0.147.0, opencode 1.18.18.

Common to all three:

- The run home is `<runs_root>/<run_id>/home/`; `home/stack/` is a full copy of the stack (the target of
  `${stack}`).
- The prompt's first user message is written to the process's **stdin**; the system prompt goes into a file.
- The child environment is the host environment minus `ANTHROPIC_*`, `CLAUDE_*`, `CODEX_*`, `OPENAI_*`,
  `OPENCODE_*`, `XDG_*`, plus the variables listed per harness below.
- Every harness also gets PHD's npm settings, each in lower and upper case with one value: `npm_config_cache` =
  `~/.phd/cache/npm/cache`, and `npm_config_userconfig` = `npm_config_globalconfig` = the empty file
  `~/.phd/cache/npm/npmrc`. So the agent's own `npm` commands (and opencode's embedded npm) use PHD's npm cache and
  ignore the host's `~/.npmrc` and global npmrc; a project `.npmrc` in `cwd` still applies. Private registries are
  not reachable this way until a stack-level npmrc provides them (planned). See
  [Security and isolation](security-and-isolation.md).
- `argv[0]` is resolved on `PATH` at launch (`.cmd` / `.bat` shims on Windows run as `COMSPEC /c <shim> ...`),
  unless `Dispatcher(executables={harness: [...]})` overrides it. The model catalogue command uses the same
  executable, so a wrapper must support `debug models` (codex) / `models` (opencode), or pass `validate_model=False`.
- With hooks, `home/phd/` holds `hook_shim.py`, `hooks.resolved.json` and `shim_config.json` ([Hooks](hooks.md)).
- Each adapter declares the paths it writes (`owned_paths`). Resuming a session re-materializes the same home: those
  paths plus `stack/` and `phd/` are deleted and written again from the resuming `Spec`; everything else in the
  home is the harness's and is left alone ([Resume](run-lifecycle.md#resume)).

## claude

**Run home**

```
home/
  system-prompt.md      identity + rules (+ project context)
  settings.json         permissions, builtin suppression, hooks, merged harness_config
  mcp.json              {"mcpServers": {...}} with placeholders resolved
  skills/<name>/        stack skills
  phd/                  hook runtime (only with hooks)
  stack/                stack copy
  ...                   Claude Code's own state (sessions, projects/...) written during the run
```

**Command line** (in this order; bracketed parts are conditional)

```
claude -p --output-format stream-json --verbose --model <model>
       --append-system-prompt-file <home>/system-prompt.md
       --strict-mcp-config --mcp-config <home>/mcp.json
       --setting-sources user                      (project_config="full": user,project,local)
       --permission-mode acceptEdits               (autonomy="full": --dangerously-skip-permissions)
       [--max-budget-usd <budget_usd>]
       [--json-schema '<output_schema as JSON>']
```

**Environment set:** `CLAUDE_CONFIG_DIR=<home>`, `DISABLE_AUTOUPDATER=1`, plus the credential variable and the
common npm settings (above).

**Credentials:** `claude_oauth_token` → `CLAUDE_CODE_OAUTH_TOKEN`; `anthropic_api_key` → `ANTHROPIC_API_KEY`;
`claude_host_login` → `CLAUDE_CODE_OAUTH_TOKEN` = the host file's current access token.

**`settings.json`** (before `harness_config` is merged in):

| Key | Value | When |
|---|---|---|
| `permissions.allow` | `["Skill", "mcp__<server>", ...]` (one entry per MCP server) | always |
| `disableBundledSkills` | `true` | `harness_builtins=False` |
| `enabledPlugins` | `{"cc-plugin-agents-md@builtin": false, "cc-plugin-telemetry@builtin": false, "cc-plugin-plugin-authoring@builtin": false}` | `harness_builtins=False` |
| `permissions.deny` | `["Skill(design)", "Skill(doctor)"]` | `harness_builtins=False` |
| `hooks` | `{"PreToolUse": [...], ...}` | stack has hooks |

**Identity and rules:** `--append-system-prompt-file`, appended to Claude Code's default system prompt.
**Skills:** `home/skills/`. **MCP:** the stack's servers, resolved, written unchanged in Claude's schema;
`--strict-mcp-config` ignores every other MCP source. **Hooks:** `settings.json` `hooks`
([Hooks](hooks.md#per-harness-translation)).

**Dependency seed.** On every start opencode (1.18.18) runs an in-process npm install of `@opencode-ai/plugin`
into each config folder it loads, `config/` (`OPENCODE_CONFIG_DIR`) and `xdg-config/opencode/`, and when any plugin
is configured it waits for both before loading plugins. PHD's hooks are a plugin, so a fresh home with hooks spent
17 to 100+ seconds (and 122 MB) on that install before the first event; an install cut off by a kill also left a
lock that made the next turn in the same home wait about 60 s more. opencode skips the install when the folder has
`node_modules/` and its `package-lock.json` lists, under `packages[""]`, every dependency of `package.json` plus
`@opencode-ai/plugin` (names only). So PHD writes exactly that into both folders: an empty `node_modules/`, a
`package.json` and a minimal `package-lock.json`, only when no `package-lock.json` exists yet (a resumed home, or a
real install by opencode, is never touched; a `package.json` left by a cut-off install is kept and listed). The
generated `phd-hooks.js` imports nothing from that package, so nothing is lost. The catalogue home gets the same seed.
If a later opencode checks more than names, it just installs again: slower, but correct. The npm variables above keep
such an install in PHD's cache (see [Security and isolation](security-and-isolation.md#the-phd-cache-phdcache)).

**Resume:** the home is re-materialized from the resuming `Spec`, and the argv is that fresh argv plus `--resume <session_id>`, same `CLAUDE_CONFIG_DIR` (sessions live under `projects/`, which PHD never touches); see [what a resumed turn reuses](run-lifecycle.md#what-a-resumed-turn-reuses-vs-takes-from-the-new-spec).

**Stream and result:** JSONL. `system/init` gives `session_id` and `claude_code_version` (`cli_version`); the
last `type == "result"` line gives `result` (`result_text`), `total_cost_usd`, `usage`, `num_turns`,
`structured_output`, `is_error` / `subtype`.

**Cost:** `total_cost_usd` from the result line, `cost_source = "harness"` (or `"unknown"` if absent). Cost is
only known at the end of the run. On `--resume`, `total_cost_usd` is the **session's** cumulative cost (token usage
is still per invocation), so PHD reports `cost_usd` = reported − the session's cost before this turn (the previous
run's `session_cost_usd`, or its `cost_usd` for an older record) and keeps the reported total in
`session_cost_usd`. `--max-budget-usd` caps one invocation, which matches PHD's per-turn budget check.

**Model catalogue:** no CLI command. PHD accepts the aliases `opus`, `sonnet`, `haiku`, `fable` and the known ids
`claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-opus-5`, `claude-sonnet-5`,
`claude-haiku-4-5`, `claude-haiku-4-5-20251001`; a trailing context suffix like `[1m]` is ignored. Any other
`claude-...` id is accepted with a warning; anything else raises `ModelNotSupported`.

**Quirks**

- On Windows Claude Code runs shell commands through its `PowerShell` tool, so the neutral `shell` hook matcher is
  `Bash|PowerShell` (and `edit` is `Edit|MultiEdit`).
- `design` and `doctor` skills cannot be switched off; with `harness_builtins=False` they still appear in the
  skill list but are denied.
- Claude's auto-memory writes under `home/projects/<git-root-key>/memory/`: inside the run home, and visible to a
  resumed run (CoreDesign §14.2).

## codex

**Run home**

```
home/
  AGENTS.md             identity + rules (+ project context): codex global instructions
  config.toml           model, approval/sandbox, MCP, skill disabling, merged harness_config
  hooks.json            {"hooks": {...}} (only with hooks)
  auth.json             copy of the credential file (mode 0600 on POSIX)
  output-schema.json    (only with output_schema)
  skills/<name>/        stack skills
  phd/                  hook runtime (only with hooks)
  stack/                stack copy
```

**Command line**

```
codex exec --json --strict-config --skip-git-repo-check -C <cwd> -m <model>
           [--ignore-rules]                       (project_config != "full")
           [--dangerously-bypass-hook-trust]      (stack has hooks)
           [--output-schema <home>/output-schema.json]
           -                                      (prompt on stdin)
```

**Environment set:** `CODEX_HOME=<home>` and the common npm settings (above). **Credentials:** `codex_auth_file` is copied to `home/auth.json`
(again on every resume).

**`config.toml`** (before `harness_config`):

| Key | Value | When |
|---|---|---|
| `model` | `<model>` | always |
| `approval_policy` | `"never"` | always |
| `sandbox_mode` | `"workspace-write"` / `"danger-full-access"` | `autonomy` restricted / full |
| `project_doc_max_bytes` | `0` (suppresses repo `AGENTS.md`) | `project_config != "full"` |
| `[mcp_servers.<name>]` | stdio: `command`, `args`, `env` (if set); http: `url`, `http_headers` (from `headers`); always `default_tools_approval_mode = "approve"` | stack has MCP servers |
| `[[skills.config]]` | `path = "<.../SKILL.md>"`, `enabled = false` for each foreign skill | foreign skills found |
| `[skills.bundled]` | `enabled = false` (codex's `.system` skills) | `harness_builtins=False` |

*Foreign skills* are `SKILL.md` files codex would discover outside the run home: `~/.agents/skills/*/` always
(codex resolves the user home through the OS, so it cannot be redirected), plus `.agents/skills/*/` in `cwd` and
every parent directory unless `project_config="full"`.

`default_tools_approval_mode = "approve"` is required: under `approval_policy = "never"` codex otherwise cancels
MCP tool calls. `--strict-config` makes unknown config keys fail, so a typo in `harness_config` fails the run.

**Identity and rules:** `home/AGENTS.md`. **Skills:** `home/skills/`. **Hooks:** `home/hooks.json` in Claude's
schema; `--dangerously-bypass-hook-trust` because PHD authored them.

**Resume:** `codex exec resume <session_id>`, then the flags kept from the fresh argv (`--json`,
`--strict-config`, `--skip-git-repo-check`, `--ignore-rules`, `--dangerously-bypass-hook-trust`, and
`--output-schema <path>` when present), then `-m <model> -`. There is no `-C`: the working directory comes from
the process (`Spec.cwd`). All of it, and `config.toml` (sandbox, approvals, MCP), comes from the resuming `Spec`,
re-materialized into the same `CODEX_HOME` (`sessions/` and `*.sqlite` are kept); see [what a resumed turn reuses](run-lifecycle.md#what-a-resumed-turn-reuses-vs-takes-from-the-new-spec).

**Stream and result:** JSONL. `thread.started.thread_id` is the session id; the last completed `agent_message`
item is `result_text`; each `turn.completed` adds `usage` and counts a turn (and clears an earlier error);
`turn.failed` / `error` set the error. `cli_version` is not reported (`None`).

**Cost:** codex reports none: `cost_usd = None`, `cost_source = "unknown"`. `budget_usd` cannot be enforced
(a warning is added at `Dispatcher(...)`).

**Model catalogue:** `codex debug models` (JSON, the `slug` of each model). If the command fails, a warning is
added and the model is not checked. The lookup is lazy (first `start()` / `plan()`, in a worker thread) and cached
per process, so `ModelNotSupported` comes from `start()` / `run()` / `plan()`, not from `Dispatcher(...)`. The default catalogue uses the same executable as the run
(`executables[harness]`), so a wrapper script must support `debug models` / `models`, or pass `validate_model=False`.
The command runs isolated: the run's stripped environment, a throwaway
`CODEX_HOME` = `~/.phd/cache/codex/catalog-home/<id>/<uuid>/` per load (holding a copy of the spec's `auth.json`,
deleted when the command ends; without it codex lists only its bundled models), and that folder as cwd. It never
touches the host's `~/.codex`
([Security and isolation](security-and-isolation.md#the-phd-cache-phdcache)).

**Quirks**

- **Temp-dir refusal:** codex refuses to create helper binaries when `CODEX_HOME` is under the system temp
  directory. `Dispatcher` raises `InvalidRunsRoot` for a codex `runs_root` under `tempfile.gettempdir()` unless
  `allow_temp_runs_root=True` (used by the offline tests with a fake harness).
- **cmd.exe hook quoting (Windows):** codex runs hook commands through `cmd.exe /C`, which strips the first and
  last quote of a line that starts with a quote. PHD quotes only arguments that need it, and when the Python
  interpreter path itself needs quoting it wraps the whole command in one extra pair of quotes. The spaced
  interpreter case is verified against `cmd.exe` directly, not against real codex (CoreDesign §14.2, open item 15).
- `--dangerously-bypass-hook-trust` makes codex emit a warning `error` item; the parser drops it.
- Hook tool matching: only `shell` (`Bash`) is verified for codex; other neutral tools raise `HookUnsupported`.
- Blocking hooks work, but codex reports only "rejected: blocked by policy", not the hook's reason.
- `http` MCP translation (`url` + `http_headers`) is unverified; PHD adds a warning.

## opencode

**Run home**

```
home/
  AGENTS.md                  identity + rules (+ project context)
  config/                    OPENCODE_CONFIG_DIR
    opencode.json            OPENCODE_CONFIG: instructions, mcp, permission, autoupdate, merged harness_config
    skills/<name>/           stack skills
    plugin/phd-hooks.js      generated hook plugin (only with hooks)
    package.json, package-lock.json, node_modules/   dependency seed (below)
  data/                      XDG_DATA_HOME (sessions; needed for resume)
  xdg-config/                XDG_CONFIG_HOME
    opencode/                package.json, package-lock.json, node_modules/: dependency seed
  state/                     XDG_STATE_HOME
  phd/                       hook runtime (only with hooks)
  stack/                     stack copy
```

**Command line**

```
opencode run --format json -m <provider/model> --dir <cwd> [--auto]     (--auto when autonomy="full")
```

**Environment set:**

| Variable | Value |
|---|---|
| `OPENCODE_CONFIG` | `<home>/config/opencode.json` |
| `OPENCODE_CONFIG_DIR` | `<home>/config` |
| `OPENCODE_DISABLE_CLAUDE_CODE` | `1` (do not read Claude Code's files) |
| `OPENCODE_DISABLE_EXTERNAL_SKILLS` | `1` |
| `OPENCODE_DISABLE_AUTOUPDATE` | `1` |
| `OPENCODE_DISABLE_PROJECT_CONFIG` | `1`, unless `project_config="full"` |
| `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME` | `<home>/data`, `<home>/xdg-config`, `<home>/state` |
| `OPENCODE_AUTH_CONTENT` | the auth file as JSON, filtered to `providers` |
| `XDG_CACHE_HOME` | the shared PHD cache of this opencode install ([PHD cache](security-and-isolation.md#the-phd-cache-phdcache)) |
| `npm_config_cache`, `npm_config_userconfig`, `npm_config_globalconfig` | the common npm settings (above) |

**`opencode.json`** (before `harness_config`):

| Key | Value |
|---|---|
| `$schema` | `https://opencode.ai/config.json` |
| `instructions` | `["<home>/AGENTS.md"]` |
| `mcp.<name>` | stdio → `{"type": "local", "command": [command, *args], "environment": env, "enabled": true}`; http → `{"type": "remote", "url": url, "headers": headers, "enabled": true}` |
| `permission` | restricted: `{"edit": "allow", "bash": {"*": "deny"}, "external_directory": "deny"}`; full: `{"*": "allow"}`; plus `"skill": {"customize-opencode": "deny"}` when `harness_builtins=False` |
| `autoupdate` | `false` |

**Hooks:** a generated plugin `config/plugin/phd-hooks.js` (only `pre_tool` and `post_tool`;
[Hooks](hooks.md#opencode-plugin)).

**Resume:** the home is re-materialized from the resuming `Spec`, and the argv is that fresh argv plus `--session <session_id>`, same `XDG_DATA_HOME` (`data/`, which PHD never touches); see [what a resumed turn reuses](run-lifecycle.md#what-a-resumed-turn-reuses-vs-takes-from-the-new-spec).

**Stream and result:** JSON events. `sessionID` (first seen) is the session id; the last `text` part is
`result_text`; each `step_finish` adds tokens and `cost` and counts a turn; a non-retryable `error` sets the error
(retryable ones become `log` warnings). `cli_version` is `None`.

**Cost:** the running sum of `step_finish.cost`; `cost_source = "harness"` always (`cost_usd` stays `None` if no
step finished). Because cost streams, PHD stops the run as soon as it exceeds `budget_usd`.

**Model catalogue:** `opencode models` (lines containing `/` and no space). A model missing from it is only a
**warning**, not `ModelNotSupported`: the list comes from a seeded or bundled models.dev snapshot that can lag behind,
so the run goes ahead and a truly unknown model fails after launch as `error.kind="model_unavailable"`. It runs
isolated: the run's stripped
environment, the spec's credentials as `OPENCODE_AUTH_CONTENT` (without them only free models are listed), data,
config and state folders under `~/.phd/cache/opencode/catalog-home/` (also its cwd, so no project is registered for
your directory), project config off, the dependency seed and npm variables of a run, and the shared PHD cache of that opencode
install as `XDG_CACHE_HOME`. The
same shared cache is every run's `XDG_CACHE_HOME` ([Security and isolation](security-and-isolation.md#the-phd-cache-phdcache)).

**Quirks**

- `output_schema` has no native flag in `opencode run` (its `format: json_schema` exists only in the SDK/server API):
  PHD appends an instruction to the system prompt (`AGENTS.md`) and validates the answer itself, like for every
  harness ([combinations](combinations.md#output_schema)).
- Only `pre_tool` and `post_tool` hooks; `session_start` / `stop` raise `HookUnsupported`.
- `customize-opencode` is hardcoded with no off switch; the permission deny removes it from the model's skill list,
  but `opencode debug skill` still lists it.
- Free models (for example `opencode/space-bunny-free`, used by the live tests; earlier `opencode/big-pickle`)
  come and go. If a run fails with `model_unavailable` or `billing` (for example `402 Insufficient account
  funds`), list the current models with `opencode models`.

## Built-in skill suppression (`harness_builtins`)

| Harness | `harness_builtins=False` (default) writes | Residual |
|---|---|---|
| claude | `disableBundledSkills: true`; the three `cc-plugin-*@builtin` plugins disabled; `permissions.deny: ["Skill(design)", "Skill(doctor)"]` | `design`, `doctor` still listed, but denied |
| codex | `[skills.bundled] enabled = false` | none |
| opencode | `permission.skill = {"customize-opencode": "deny"}` | still enumerated by `opencode debug skill` |

With `harness_builtins=True` none of these keys are written. Foreign-skill disabling for codex and
`OPENCODE_DISABLE_EXTERNAL_SKILLS` / `OPENCODE_DISABLE_CLAUDE_CODE` for opencode apply either way. The constants
carry the CLI version they were verified against; re-check them on CLI upgrades.

---

**Next:** [Prompt assembly](prompt-assembly.md) · **See also:** [Option combinations](combinations.md), [Hooks](hooks.md), [Troubleshooting](../troubleshooting.md)
