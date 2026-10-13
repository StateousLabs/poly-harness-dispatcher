# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/) once it leaves alpha.

## [Unreleased]

### Fixed

- **Stack component names are portable on every OS.** Skill, MCP component and hook bundle names are checked with the
  same rule as stack names: no Windows device names (`con`, `nul.md`, `com1`, ...) and no trailing `.`, so a stack
  resolved on Linux or macOS also works on Windows.
- **Stack content hash covers dot-folders.** `Stack.content_hash()` and the store's identity hash skipped every file
  under a `.`-folder, although assembly and the run copy include them (only `.git/`, `__pycache__/` and `*.pyc` are
  left out). The hash now skips exactly those (anything named `.git` or `__pycache__`, and `*.pyc`). Dot-folders
  such as `.venv/` or `.pytest_cache/` in a component source were already copied and now count: keep them out of sources. A stack already stored with a hash from the old rule is re-hashed on
  `resolve` and its index hash updated, instead of raising `StackConflict`.
- **Session lock takeover race.** Two runs that found the same stale `.phd-session.lock` could both take it over: one
  deleted the lock the other had just written, and both ran on the same session. Taking over a stale lock is now
  serialized by an OS lock on `<home>/.phd-session.lock.takeover` and re-checks the lock before deleting it; `release` now deletes its lock before it stops counting as held.
- **`prune` no longer deletes a running resolve's staging folder.** A resolve that took longer than an hour (slow
  downloads) lost its folder to a concurrent `stacks.prune()`. Each staging folder now records its owner process
  (`.owner.json`); `prune` keeps folders whose owner is still running and applies the one-hour rule to the rest.
- **`from phd.stacks import *` no longer shadows the builtin `list`.** `list` is out of `phd.stacks.__all__`;
  `phd.stacks.list()` works as before.
- **The streamed `usage` event's cost matches `RunResult`.** On a resumed claude turn the `usage` event (and its line
  in `events.jsonl`) carried claude's session-cumulative `total_cost_usd` as `cost_usd`. It now carries the turn's own
  cost, computed by the same rule as `RunResult.cost_usd` (a repair turn's events add up to the run's `cost_usd`), plus
  `session_cost_usd` (claude's cumulative figure; `None` for codex and opencode, whose `cost_usd` is unchanged).
- **The codex `auth.json` copy no longer stays at rest.** It stayed in `<runs_root>/<run_id>/home/auth.json` after
  the turn. It is now deleted when the turn ends, however it ends (also after a failed spawn and after a dry run,
  which no longer leaves one), and re-copied on resume. Copies left by a killed PHD process are deleted by the first
  `start()` / `plan()` of the next process using the same `runs_root`, except in homes a live run holds; the sweep never follows a symlink or junction, and a resume that meets its brief hold (or any lock file still being written) waits up to 2 s before it reports busy. Adapters
  declare their credential files with a new `secret_paths(home)` method. If codex refreshes its token mid-turn, the
  refreshed token is discarded (see Security and isolation, Known limitations).
- **Credential files are copied byte for byte.** The codex `auth.json` (run home and model-catalogue home) was read
  and written as text, so on Windows its LF line endings became CRLF. `write_private` now takes bytes.

### Internal

- Coverage now measures child processes (the reaper, harness stand-ins) with `patch = ["subprocess"]`; the hook shim,
  which runs from a copy in each run home, is covered by in-process tests. The CI coverage floors for Linux and macOS
  were raised to 89 (Windows stays 90). New POSIX tests cover the 5 s stderr wait (`STDERR_GRACE_S`) and run
  `reaper_main.py` directly.

## [0.1.0] — 2026-10-12

First release: layer 1, the core spawner. Agents run on the host with no sandbox.

### Added

- **Spawner.** `Dispatcher(spec).spawn()` / `start()` / `run()` launch headless agents on three harnesses:
  Claude Code (`claude -p`), Codex (`codex exec`) and OpenCode (`opencode run`), from one harness-neutral
  `Spec`, a `Stack` and per-run `Inputs`. `Credentials` (host login, OAuth token, API key, auth files),
  secrets filling `${env:NAME}` placeholders, `plan()` dry runs and a per-harness model catalogue
  (`ModelCatalog`, `ModelNotSupported`).
- **Results and events.** A normalized `RunResult` (status, cost, usage, result text, error) and a stream of
  `Event` values; a failed run does not raise. `LocalLedger` / `ResultSink` record every run under
  `~/.phd/runs/<run_id>/`; `phd.runs` reads them back.
- **Stacks.** A stack is a `config/` directory holding identity, skills, MCP servers and hooks, materialized
  into an isolated config home for every run. Hooks (`HookDef`) are translated per harness.
- **Stack resolver.** `phd.stacks`: `compose()` from a `stack.toml` manifest (base stack plus extra skills, MCP
  servers and hooks, from local paths or `https://` sources, archives unpacked safely), `resolve`, `get`, `list`,
  `remove`, `prune`, and a local name/version store with identity-hash dedupe and conflict detection
  (`StackConflict`, `StackNotFound`, `StackFetchError`). Runs whose stack was removed are refused.
- **Resume.** `session_id=` continues a harness session; the run home is locked so two turns never share a
  session at once. A session whose run crashed before writing its result can be resumed (`session.json` is
  recorded as soon as the session starts), and the crashed turn's cost is carried into the next claude turn.
  A crashed run with `resumable=False` is not resumed.
- **Run lifecycle.** `Dispatcher.run()` async context manager ties a run to a block and cancels it on exit; the
  `session.started` event carries the session id at start; `Spec.idle_timeout_s` stops silent runs; orphan
  protection kills a harness whose caller dies (Windows Job Object, POSIX reaper); a fast, bounded stop that
  holds even for harnesses that ignore signals; the event loop is never blocked (lazy per-process model
  catalogue, off-loop planning, non-blocking `taskkill`); `kind="session"` marks a run whose session is gone
  or cannot be resumed.
- **Structured output.** `Spec.output_schema` is validated with `jsonschema` on every harness and translated per
  harness (wrapper for non-object roots, Codex strict mode with refusal of inexpressible forms, OpenCode
  support, answer restored to the caller's shape), with an opt-in schema repair turn (`schema_repair`).
- **Host isolation of the model catalogue and caches.** `codex debug models` / `opencode models` run with the
  stripped environment, PHD-owned homes under `~/.phd/cache` (`PHD_CACHE_DIR`) and the spec's credentials, so
  they no longer touch the host's `opencode.db` or `~/.codex/models_cache.json`; the catalogue cache is keyed
  per credential. OpenCode runs share one PHD-owned `XDG_CACHE_HOME` per opencode install instead of the host's
  `~/.cache/opencode`. The codex catalogue copies `auth.json` (refresh token included) into a throwaway
  `CODEX_HOME` under `~/.phd/cache` only while `codex debug models` runs, and deletes it afterwards.
  A catalogue timeout kills the command's whole process tree; homes left by a killed process are swept
  after 10 minutes.
- **`Event.summary`.** A one-line, secret-scrubbed summary of messages and tool events.
- **Stable error kinds.** `ERROR_KINDS` and the `PhdError` hierarchy (`InvalidSpec`, `MissingCredentials`,
  `NotResumable`, `HarnessNotInstalled`, `UnsafeAutonomy`, ...); problems detectable before launch raise.
- Typed package (`py.typed`), a documentation site (mkdocs) and an offline test suite built on a fake harness.

### Changed

- `ModelCatalog.models()` / `validate()` take the credentials to list with. `ModelCatalog().models("opencode")`
  without credentials lists only the free models (it used to use the host login).
- An opencode model missing from the catalogue is a warning, not `ModelNotSupported` (the catalogue can lag behind);
  an unknown model then fails after launch as `error.kind="model_unavailable"`. Codex still raises.
- A catalogue that cannot be set up (unusable `PHD_CACHE_DIR`, disk errors) is the usual "could not load the model
  catalogue" warning.
- OpenCode runs with hooks start in seconds: PHD seeds both config folders (and the catalogue home) with the state
  opencode treats as "dependencies installed", so it no longer npm-installs `@opencode-ai/plugin` (17 to 100+ s and
  122 MB per fresh home, plus a 60 s stale-lock wait after a killed install).
- Every harness's run (and the opencode catalogue) gets PHD's npm settings: `npm_config_cache` =
  `~/.phd/cache/npm/cache` and an empty PHD-owned npmrc as `npm_config_userconfig` / `npm_config_globalconfig`.
  Agents' own `npm` commands on claude, codex and opencode therefore use PHD's npm cache and ignore the host
  `~/.npmrc`, so private registries are not reachable unless a future stack-level npmrc provides them (planned).

[0.1.0]: https://github.com/StateousLabs/poly-harness-dispatcher/releases/tag/v0.1.0
