# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/) once it leaves alpha.

## [0.1.0] — unreleased

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
  122 MB per fresh home, plus a 60 s stale-lock wait after a killed install). opencode's npm uses a PHD-owned cache
  and an empty npmrc (`npm_config_cache`, `npm_config_userconfig`, `npm_config_globalconfig`), never the host's npm
  cache or `~/.npmrc`; the agent's own `npm` commands inherit these.

[0.1.0]: https://github.com/StateousLabs/poly-harness-dispatcher/releases/tag/v0.1.0
