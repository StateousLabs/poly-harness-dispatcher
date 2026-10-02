# Security and isolation

This page states what PHD isolates today and how, what it does **not** protect without a sandbox, how secrets
are handled, and the known limitations.

PHD layer 1 runs every agent **on the host, as your user, with no sandbox**. Its isolation is about
*configuration hermeticity* (a run sees only what PHD gives it), not about containing a hostile agent. A sandbox
layer is planned ([Architecture](../architecture.md#roadmap-components)).

## What is hermetic today, and how

| Concern | Mechanism | Scope |
|---|---|---|
| Host user-level harness config (`~/.claude`, `~/.codex`, `~/.config/opencode`: global hooks, plugins, memory, global instructions, settings) | each run gets its own config home: `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `OPENCODE_CONFIG` + `OPENCODE_CONFIG_DIR` + `XDG_DATA_HOME` / `XDG_CONFIG_HOME` / `XDG_STATE_HOME` | all harnesses |
| Host user-level skills | separate config home; codex: every `~/.agents/skills/*/SKILL.md` disabled by path in `config.toml` (codex resolves the user home via the OS); opencode: `OPENCODE_DISABLE_EXTERNAL_SKILLS=1`, `OPENCODE_DISABLE_CLAUDE_CODE=1` | all harnesses |
| Other MCP sources | claude `--strict-mcp-config` (only `home/mcp.json`); codex and opencode: their user-level config is the generated one in the run home | all harnesses |
| Host harness/auth environment variables | the child env drops every `ANTHROPIC_*`, `CLAUDE_*`, `CODEX_*`, `OPENAI_*`, `OPENCODE_*`, `XDG_*` variable before PHD adds its own | all harnesses |
| Repo instructions and config in `cwd` | `project_config="ignore"` (default): claude `--setting-sources user`; codex `project_doc_max_bytes=0`, `--ignore-rules`, repo `.agents/skills` disabled; opencode `OPENCODE_DISABLE_PROJECT_CONFIG=1` | unless `project_config="full"` |
| Harness built-in skills and plugins | `harness_builtins=False` (default); see [Harnesses](harnesses.md#built-in-skill-suppression-harness_builtins) | residual: claude `design`/`doctor` listed but denied |
| Self-updates during a run | claude `DISABLE_AUTOUPDATER=1`; opencode `OPENCODE_DISABLE_AUTOUPDATE=1` and `"autoupdate": false` | claude, opencode |
| Runs interfering with each other | separate run directory and home per run; the stack is copied, never modified | all |

The live test `test_self_check_is_hermetic` runs each harness in a `cwd` with poisoned `CLAUDE.md` and
`AGENTS.md` and checks that the agent ignores them while identity, rules, both skills, the MCP server, the shell
hook and the hook log all work. (It poisons the repo, not the global user config.)

## What is NOT protected without a sandbox

| Area | Reality today |
|---|---|
| **Filesystem** | The harness, its shell commands, MCP servers and hooks run as your user and can read and write whatever you can. `restricted` relies on each harness's permission system (claude `acceptEdits`, codex `workspace-write`, opencode permissions); `full` removes even that. |
| **Network** | Unrestricted for the harness and everything it starts. |
| **`restricted` is not a security boundary** | It is a harness setting. PHD does not trust it as isolation; that is why `autonomy="full"` needs `unsafe=True` and why a sandbox layer is planned. |
| **Home directory** | `HOME` / `USERPROFILE` are not changed, so tools the agent runs (git, gh, ssh, cloud CLIs) use your real configuration and credentials. |
| **Other environment variables** | Only the six prefixes above are stripped. `GITHUB_TOKEN`, `AWS_*`, other `*_API_KEY` variables and the rest of your environment reach the agent (CoreDesign §14.2, open item 11). |
| **Repo code** | `project_config="full"` loads the repo's hooks, settings and MCP servers: code PHD does not control. |
| **Hook commands and MCP servers** | Run on the host with the harness's environment, which includes the injected credential variables. |
| **Leftover processes** | On Windows, descendants of a harness that already exited cannot be killed by `taskkill /T`. |

## Secret handling

**What PHD keeps out of logs and records**

| Item | Protection |
|---|---|
| `Credentials.secret`, `Spec.secrets`, `MaterializedHome.env` | excluded from `repr()` |
| `run.json` `env` | values of variables whose name contains `TOKEN`, `KEY`, `AUTH_CONTENT`, `SECRET` or `PASSWORD` (case-insensitive) replaced with `<redacted>` |
| `result.json` | records only `credential_kind`, never the credential |
| `harness_crash` message | the stderr tail is scrubbed: every value of `Spec.secrets` and `Credentials.secret` is replaced with `<redacted>` |
| claude host login | only the access token is used; the refresh token is never copied |
| codex `auth.json` copy | created with mode 0600 on POSIX (no POSIX mode on Windows) |

**Where secrets do end up in plaintext**

| Location | What |
|---|---|
| run home config files | `${env:NAME}` values from `Spec.secrets`, resolved into claude `mcp.json`, codex `config.toml`, opencode `opencode.json`, and `phd/hooks.resolved.json` |
| `home/auth.json` (codex) | the full codex auth file, including any refresh token |
| the harness process environment | `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` or `OPENCODE_AUTH_CONTENT` |
| `events.jsonl`, `stderr.log`, `result_text` | whatever the harness prints, unfiltered: if the agent echoes a secret it is recorded |

The run home is deleted after the run only with `resumable=False` (and never by a resumed turn, which shares the
session's home). Otherwise these files stay under `runs_root`
until you delete them; PHD does not prune run directories (open item 13).

Scrubbing gaps: the stderr scrub uses exact string replacement of `Spec.secrets` values and
`Credentials.secret`. A `claude_host_login` access token and `OPENCODE_AUTH_CONTENT` are not
`Credentials.secret`, so they are not scrubbed from that message if a harness printed them.

## Known limitations

- No sandbox; filesystem and network are open (above).
- Windows orphaned descendants after the direct child exits (no Job Object).
- Non-prefixed provider keys are passed to the child (open item 11).
- claude restricted mode has no PHD deny rules for paths outside `cwd` (open item 7).
- Claude auto-memory is written inside the run home and visible to resumed runs; there is no switch yet
  (open item 5).
- Run directories are never pruned (open item 13).

---

**Next:** [Architecture](../architecture.md) · **See also:** [Run lifecycle](run-lifecycle.md), [Harnesses](harnesses.md)
