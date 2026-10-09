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
| Host harness caches | opencode: `XDG_CACHE_HOME` is the shared PHD cache `~/.phd/cache/opencode/xdg-cache/<install>/` (models.json, ripgrep, plugin packages), never the host's `~/.cache/opencode`; codex and claude keep their caches in the run home | all harnesses |
| Host npm cache and npm config | opencode's embedded npm (it installs `@opencode-ai/plugin` into config folders; PHD seeds them so it normally does not run, see [Harnesses](harnesses.md#opencode)) gets `npm_config_cache` = `~/.phd/cache/opencode/npm-cache` and an empty PHD-owned npmrc as user and global config, so it never writes `%LOCALAPPDATA%
pm-cache` / `~/.npm` nor reads `~/.npmrc` | opencode runs and catalogue |
| Model catalogue lookup (`codex debug models`, `opencode models`) | runs isolated too: same stripped environment, PHD-owned homes and cwd under `~/.phd/cache/<harness>/catalog-home/`, and the spec's credentials. It never opens the host's `opencode.db` or `~/.codex/models_cache.json` and registers no project for your directory | codex, opencode |
| Repo instructions and config in `cwd` | `project_config="ignore"` (default): claude `--setting-sources user`; codex `project_doc_max_bytes=0`, `--ignore-rules`, repo `.agents/skills` disabled; opencode `OPENCODE_DISABLE_PROJECT_CONFIG=1` | unless `project_config="full"` |
| Harness built-in skills and plugins | `harness_builtins=False` (default); see [Harnesses](harnesses.md#built-in-skill-suppression-harness_builtins) | residual: claude `design`/`doctor` listed but denied |
| Self-updates during a run | claude `DISABLE_AUTOUPDATER=1`; opencode `OPENCODE_DISABLE_AUTOUPDATE=1` and `"autoupdate": false` | claude, opencode |
| Runs interfering with each other | separate run directory and home per run; the stack is copied, never modified | all |

### The PHD cache (`~/.phd/cache`)

PHD keeps a small persistent cache outside every run home. `PHD_CACHE_DIR` moves it (default `~/.phd/cache`):

| Folder | Used by | Contents |
|---|---|---|
| `opencode/xdg-cache/<install>/` | every opencode run and the opencode catalogue (`XDG_CACHE_HOME`) of one opencode install | `opencode/models.json` (models.dev list, refreshed by opencode when older than 5 minutes), `opencode/bin/` (ripgrep) and the npm packages opencode installs for plugins named in its config. `<install>` is a short hash of the resolved executable (path, size, mtime; the `cmd.exe /c` wrapper of a `.cmd` shim is left out, so a Windows update keeps the cache), so different installs never overwrite each other's cache. Shared by all runs of that install, so everything is downloaded once, not per run; this also means **plugins named via `harness_config` are installed once and shared across runs**. On first use PHD copies the host's `~/.cache/opencode/models.json` in (read only on the host), so the first catalogue is complete; refresh temp files older than an hour are pruned. |
| `opencode/npm-cache/`, `opencode/npmrc` | every opencode run and the opencode catalogue (`npm_config_cache`, `npm_config_userconfig`, `npm_config_globalconfig`) | the npm cache of any install opencode's embedded npm still does (none with the dependency seed), and an empty npmrc that stands in for the user's `~/.npmrc` and the global npmrc. Shared by all installs; npm's cache is content-addressed and safe for concurrent use. |
| `opencode/catalog-home/` | `opencode models` | the catalogue's own data, config and state folders (its `opencode.db`, logs, locks). No credential: the login travels in `OPENCODE_AUTH_CONTENT`. |
| `codex/catalog-home/<id>/<uuid>/` | `codex debug models` | a throwaway `CODEX_HOME` per catalogue load (`<id>` is a hash of the credential, never the secret or path) holding a copy of the spec's `auth.json`. Deleted as soon as the command finishes, also when it fails or times out (a timeout kills the command's whole process tree); folders left by a killed PHD process are swept by the next load once they are 10 minutes old, and a failed deletion is reported as a catalogue warning. Not under the temp dir: codex refuses a temp `CODEX_HOME` (PHD warns once if `PHD_CACHE_DIR` is under it). |

The catalogue list depends on the login (opencode lists only free models without one), so it is cached per
process per harness, executable **and credential**.

`HOME` / `USERPROFILE` are deliberately **not** redirected: the agent's git identity, ssh keys and other tool
config must keep working. Harness state is isolated with the variables above instead. One exception for opencode:
the npm variables above are inherited by the agent's own shell commands, so `npm` run by an opencode agent uses PHD's
npm cache and ignores `~/.npmrc` (a project `.npmrc` in `cwd` still applies); set your own `npm_config_*` through a
stack or the command if an agent needs a private registry.

The live test `test_a_run_leaves_the_host_harness_folders_untouched` (`tests/live/test_host_isolation.py`) runs
one real `spawn` per harness, catalogue included, and checks that nothing in `~/.codex`, `~/.local/share/opencode`,
`~/.cache/opencode`, `~/.local/state/opencode` or `~/.config/opencode` changed (files by size and mtime, folders by
presence) and that Claude registered no project for the run's `cwd`.

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
| **Leftover processes** | Windows: a Job Object kills descendants on cancel, on timeouts and when the harness exits; children that try to break away from the job are refused. POSIX: `killpg` covers cancel and timeouts, and a reaper process kills the harness's group if the caller dies. Gaps on POSIX: the short window before the reaper starts, descendants that call `setsid`, and a normal finish does not kill leftovers. See [Orphan protection](run-lifecycle.md#orphan-protection). |

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
| `~/.phd/cache/codex/catalog-home/<id>/<uuid>/auth.json` | the full codex auth file, including the refresh token, copied for the model catalogue. It exists only while `codex debug models` runs and is deleted right after (a crash of the PHD process in that window can leave it behind; the next catalogue load deletes it once it is 10 minutes old) |
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
- POSIX orphan protection has gaps: a short window between harness start and reaper start, descendants that start their own session (`setsid`) escape the reaper, and a normal finish does not kill background processes a tool left running (Windows does). If protection cannot be set up the run continues with the warning `orphan protection unavailable: ...`.
- Non-prefixed provider keys are passed to the child (open item 11).
- The PHD cache is shared by concurrent runs. opencode writes `models.json` through a temp file and a rename, so a
  reader sees the old or the new file; an interrupted refresh can leave a `models.json.<pid>.<ts>.tmp` file behind
  (harmless; PHD prunes those older than an hour). Two runs downloading ripgrep or installing the same plugin
  package at the same moment on a cold cache was not stress-tested.
- claude restricted mode has no PHD deny rules for paths outside `cwd` (open item 7).
- Claude auto-memory is written inside the run home and visible to resumed runs; there is no switch yet
  (open item 5).
- Run directories are never pruned (open item 13).
- The session lock (`<home>/.phd-session.lock`, see [Run lifecycle](run-lifecycle.md#session-lock)) decides
  "still running" by process id; if the holder died and the OS reused its pid for an unrelated live process, the
  session reports busy until that process exits or the lock file is deleted by hand.
- `LocalLedger.find_session` scans every `result.json` (and, for a session with no finished run, every `session.json`) in the ledger on each resume; fine at current scale,
  an index is needed later.
- Rules files are stored in plain text as `rules_text` in each `run.json` of a session (secrets never are).

---

**Next:** [Architecture](../architecture.md) · **See also:** [Run lifecycle](run-lifecycle.md), [Harnesses](harnesses.md)
