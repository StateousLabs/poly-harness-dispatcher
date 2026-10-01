# PHD Core Design — the spawner

> Reference document for **poly-harness-dispatcher (PHD)**, component #1: the core spawner.
> Status: **agreed in brainstorming, not yet implemented.** Items marked **[verify]** are
> assumptions about harness CLIs that must be confirmed by a spike before implementation.
> Last updated: 2026-10-01.

---

## 1. Scope

PHD is built in three layers. This document pins down **layer 1 only**.

| # | Layer | Responsibility | Status |
|---|-------|----------------|--------|
| 1 | **Core spawner** | Spec + Stack → materialized config home → headless CLI run → normalized `RunResult` | **This document** |
| 2 | Sandbox | The isolation boundary (container / hardened container / microVM / VM — undecided) | Later |
| 3 | Runtime | Creates, revives, destroys sandboxes; runs the core inside them via an in-sandbox API | Later |

**Goal for layer 1:** define the SDK, test it, and have it work **without any isolation**, on the
host, for all three harnesses.

**Design constraint — location-agnostic.** The core takes paths and returns a `RunResult`. It
knows nothing about Docker, sandboxes, or HTTP. Later, the in-sandbox API (layer 3's
`phd-runner`) is a *thin server around this same core*. The spike in `spikes/claude-docker/`
is a throwaway preview of that shape.

**Non-goal:** PHD is a dispatcher, not an orchestrator. Task graphs, dependencies, review loops
(e.g. Prodocracy's orchestrator and taskboard) stay in the consumer.

---

## 2. Components

```
            ┌──────────┐   ┌──────────┐
            │   Spec   │   │  Stack   │        (a) what/where/how      (b) who/with-what
            └────┬─────┘   └────┬─────┘
                 └──────┬───────┘
                        ▼
                 ┌──────────────┐
  Inputs ───────▶│ Materializer │  (c) pure: (spec, stack, inputs, run_dir) → MaterializedHome
  brief/msg/rules└──────┬───────┘
                        ▼
                 ┌──────────────┐
                 │   Launcher   │  (d) spawns the harness CLI with MaterializedHome
                 └──────┬───────┘
                        ▼ raw stdout/stderr stream
                 ┌──────────────┐
                 │  Collector   │  (e) harness-specific parser → events + RunResult
                 └──────┬───────┘
                        ▼
                    RunResult  ──▶ run ledger (~/.phd/runs/<run_id>/result.json)
```

Each harness (`claude`, `codex`, `opencode`) provides one **adapter** that implements the
harness-specific parts of (c), (d), and (e). Everything else is shared.

### (a) Spec — *what, where, how*

| Field | Type | Notes |
|-------|------|-------|
| `harness` | `"claude" \| "codex" \| "opencode"` | Required. Unknown → `UnknownHarness` listing valid ones. |
| `model` | `str` | Required. Validated against the harness model catalogue (§8). |
| `stack` | `Stack` | Required. The agent's identity + capabilities. |
| `cwd` | `Path` | Required. Must exist. Working directory of the agent. |
| `harness_config` | `dict` | Optional passthrough, deep-merged into the harness's own config file (`settings.json` / `config.toml` / `opencode.json`). PHD does not interpret it; PHD-owned keys win on conflict and the conflict is reported. |
| `project_config` | `"ignore" \| "instructions_only" \| "full"` | Default `"ignore"`. See §5. |
| `autonomy` | `"restricted" \| "full"` | Default `"restricted"`. See §6. `"full"` without a sandbox requires `unsafe=True`. |
| `unsafe` | `bool` | Default `False`. Explicit acknowledgement for `autonomy="full"` with no sandbox. Recorded in `RunResult`. |
| `credentials` | `Credentials` | How to authenticate this run (§7). |
| `secrets` | `dict[str, str]` | Values for `${env:NAME}` placeholders in the stack's MCP/hook config. Never written to the ledger. |
| `timeout_s` | `float` | Default 1800. Kills the whole process tree. |
| `budget_usd` | `float \| None` | Hard cap where the harness supports it (Claude `--max-budget-usd`); otherwise enforced by the collector on observed cost (best effort, reported). |
| `output_schema` | `dict \| None` | JSON Schema for a structured final answer (Claude `--json-schema`, Codex `--output-schema`; OpenCode [verify]). |
| `resumable` | `bool` | Default `True`. If `False`, the run home is deleted after the run (result kept). |

### (b) Stack — *who, with what*

A stack is a **harness-neutral** bundle: one agent identity plus its capabilities. It is
materialized per harness at spawn time. A stack is **read-only**; each run gets its own
config home built from it, so many runs can share one stack safely.

The stack is a **config dir** (layout carried over from Prodocracy's `agents/<name>/`):

```
config/
  stack.json          { name, version, description,
                        harnesses?: ["claude","codex","opencode"],   # optional compatibility list
                        default_models?: { claude: "sonnet", ... } } # suggestions; Spec.model wins
  agents/AGENTS.md    the single agent's identity / system prompt (exactly one agent per stack)
  skills/<name>/      SKILL.md + references/ + scripts/ + any other files (Agent Skills format)
  .mcp.json           neutral MCP format (§9)
  hooks/hooks.json    neutral hook events (§10) + the scripts they call (in hooks/)
```

- `Stack.load(path)` — from a directory or a `.tar.gz`.
- `Stack.compose(name, agent=..., skills=[...], hooks=[...], mcps=[...])` — assemble from the
  local component libraries `~/.phd/{agents,skills,mcps,hooks}/` and save to
  `~/.phd/local_stacks/<name>/`. Components are referenced, then copied at pack time.
- If `stack.harnesses` is set and does not include `Spec.harness` → `StackIncompatible`.
- **Remote stacks** (signed tarballs pulled into the sandbox) are a layer 2/3 concern; the core
  only ever sees an already-unpacked stack directory.

**Where things live (agreed split):**

| Item | Lives in | Why |
|------|----------|-----|
| Identity (AGENTS.md) | Stack | Defines *who* the agent is |
| Skills, hooks, MCPs | Stack | What the agent *can do* |
| Rules (RULES.md) | Inputs | Project-specific; same stack, different repos |
| Brief, msg | Inputs | The task |
| Harness, model | Spec (stack may suggest) | Caller chooses where/how it runs |
| cwd, autonomy | Spec | Never the stack author's decision |

### Inputs — *the task*

| Field | Type | Notes |
|-------|------|-------|
| `brief` | `Path \| None` | Task brief, any filename. |
| `msg` | `str \| None` | Free-form message. |
| `rules` | `Path \| None` | Binding rules file (RULES.md or any name). |

At least one of `brief` / `msg` is required → otherwise `EmptyTask`.

### (c) Materializer

A **pure function** — writes files into a fresh run directory, runs nothing:

```python
materialize(spec, stack, inputs, run_dir) -> MaterializedHome
# MaterializedHome: { home: Path, files: list[Path], env: dict, argv: list[str],
#                     stdin: str, warnings: list[str] }
```

Responsibilities:
1. Create `run_dir/home/` — the harness's isolated config home.
2. Translate the stack: `agents/AGENTS.md` → harness identity mechanism; `skills/` → harness
   skill dir; `.mcp.json` → harness MCP config; `hooks/` neutral hooks → harness hook config (or OpenCode plugin).
3. Deep-merge `harness_config`.
4. Resolve `${env:NAME}` placeholders from `spec.secrets` (into env, not into files, where possible).
5. Assemble the prompt (§4).
6. Produce `env` (config-home variables, credentials) and `argv` (hermetic flags, model,
   autonomy, output format).

Because it is pure, it is fully testable with golden files and gives **dry-run for free**:
`dispatcher.plan(inputs)` returns the `MaterializedHome` without launching.

### (d) Launcher

- Spawns `argv` with `env`, `cwd=spec.cwd`, prompt on **stdin** (Windows `cmd.exe` shims mangle
  `<`, `>`, newlines and cap argv at ~8K — lesson from Prodocracy and cib-meta-harness).
- Resolves the executable via `shutil.which`; `.cmd`/`.bat` shims run as `[COMSPEC, "/c", exe]`.
- Runs the child in its **own process group** (POSIX: new session; Windows: new process group /
  job object [verify]) so timeout and cancel kill the whole tree.
- Async-first (`asyncio`); streams stdout line-by-line into the collector.

### (e) Collector

Parses the harness's machine-readable stream into **normalized events** and, at the end, one
`RunResult`. This is where most fragility lives, so each parser is tested against recorded
real outputs (§13).

| Harness | Stream | Final data source |
|---------|--------|-------------------|
| claude | `--output-format stream-json --verbose` (JSONL) | last `type=="result"` line: `result`, `session_id`, `total_cost_usd`, `usage`, `num_turns`, `is_error`, `duration_ms` |
| codex | `exec --json` (JSONL) | `turn.completed` usage; last agent message; session/thread id [verify exact event names for 0.147] |
| opencode | `run --format json` (JSON events) | sum of `step_finish` (tokens, `cost`); `sessionID`; last `text` part; `type=="error"` → failure |

Normalized event types (minimal v1): `run.started`, `message`, `tool_call`, `tool_result`,
`usage`, `run.finished`. Every event carries `run_id`, `ts`, `harness`, and the `raw` payload.

---

## 3. Public API (sketch)

```python
from phd import Spec, Stack, Inputs, Credentials, Dispatcher

stack = Stack.load("~/.phd/local_stacks/reviewer")
spec = Spec(
    harness="claude", model="sonnet", stack=stack, cwd=r"C:\work\repo",
    project_config="instructions_only", autonomy="restricted",
    credentials=Credentials.claude_oauth_token_from_env(),
    timeout_s=1800, budget_usd=5,
)
d = Dispatcher(spec)
inputs = Inputs(brief="briefs/task-42.md", msg="focus on the parser", rules="RULES.md")

plan = d.plan(inputs)                      # dry run: files, env (redacted), argv, prompt
result = d.spawn(inputs)                   # sync wrapper → RunResult

handle = await d.start(inputs)             # async-first core
async for ev in handle.events(): ...       # normalized events
result = await handle.wait()               # or: await handle.cancel()

again = d.resume(result, msg="now fix the failing test")   # or resume(run_id, ...)
```

- Python **3.12+**, **async-first** core; `spawn()` / `resume()` are thin sync wrappers.
- **Errors before launch are exceptions** (bad spec, bad model, missing CLI, missing
  credentials). **Outcomes after launch are data** — a `RunResult` with a `status`; a failed
  run never raises.

---

## 4. Prompt assembly

| Layer | Content | Delivery |
|-------|---------|----------|
| System (identity) | `agents/AGENTS.md` of the stack | Harness identity mechanism (§6 table) |
| System (rules) | `## Project rules (binding)` + contents of `rules` | Appended to system prompt |
| System (project) | Repo `CLAUDE.md` / `AGENTS.md` — only if `project_config="instructions_only"` or `"full"` | Appended after rules, labelled as project context |
| First user message | `# Task brief` + brief contents, then `# Message` + msg | stdin |
| On resume | `msg` only | stdin; identity/rules come from the session [verify Claude re-applies `--append-system-prompt` on `--resume`] |

The brief is **inlined** by default (the agent cannot "forget to read it").

---

## 5. Project config — `project_config` (decision C)

When `cwd` is a repo with its own `CLAUDE.md`, `AGENTS.md`, `.claude/settings.json`,
`.mcp.json`, `opencode.json`, etc.:

| Mode | Repo instructions (CLAUDE.md / AGENTS.md) | Repo settings / hooks / MCPs |
|------|------|------|
| `ignore` (**default**) | Not loaded | Not loaded |
| `instructions_only` | Read by PHD and appended as project context | Not loaded |
| `full` | Loaded natively by the harness | Loaded natively — **repo hooks are code PHD does not control**; recorded in `RunResult` |

**Hermetic by default.** Independently of `project_config`, the host's *user-level* config
(`~/.claude`, `~/.codex`, `~/.config/opencode`) is **never** loaded: every run has its own
config home. Otherwise global hooks, plugins, memory, and CLAUDE.md would silently change agent
behaviour. Acceptance test: a "poisoned" global config has no effect on a PHD run (§13).

---

## 6. Per-harness mapping

| Concern | claude | codex | opencode |
|---|---|---|---|
| Config home | `CLAUDE_CONFIG_DIR=<home>` | `CODEX_HOME=<home>` | `OPENCODE_CONFIG=<home>/opencode.json`; data/auth dir via `XDG_DATA_HOME` / `XDG_CONFIG_HOME` → `<home>` [verify] |
| Base command | `claude -p --output-format stream-json --verbose` | `codex exec --json --skip-git-repo-check -C <cwd> -` | `opencode run --format json --dir <cwd>` |
| Model | `--model` | `-m` | `-m provider/model` |
| Identity (AGENTS.md) | `--append-system-prompt-file` (identity + rules) [verify flag form] | `<home>/AGENTS.md` (global instructions) | `instructions: [<home>/AGENTS.md]` in opencode.json |
| Skills | `<home>/skills/<name>/` | `<home>/skills/<name>/` [verify] | [verify location] |
| MCP | `--strict-mcp-config --mcp-config <home>/mcp.json` | `[mcp_servers.<name>]` in config.toml | `mcp` section in opencode.json |
| Hooks | `hooks` in `<home>/settings.json` | codex hooks config [verify format; hook trust — may need `--dangerously-bypass-hook-trust` or pre-trusted hooks] | generic PHD plugin (JS) in config plugin dir |
| `project_config=ignore` | `--setting-sources user` (user = our home); suppress repo CLAUDE.md [verify how — `--bare` also drops hooks, so not usable when hooks exist] | suppress repo AGENTS.md (e.g. `-c project_doc_max_bytes=0`) [verify]; `--ignore-rules` | `OPENCODE_DISABLE_PROJECT_CONFIG=1`; repo AGENTS.md suppression [verify] |
| `project_config=full` | `--setting-sources user,project,local` | default behaviour | default behaviour |
| `autonomy=restricted` | `--permission-mode acceptEdits` + deny rules outside cwd | `-s workspace-write`, `approval_policy="never"` | permission config, `external_directory: deny` |
| `autonomy=full` | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | `--auto` + permissive permission config |
| Credentials | env `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or `ANTHROPIC_API_KEY` | `auth.json` copied into `<home>` or `OPENAI_API_KEY` [verify env name for exec] | provider entry from `auth.json` into data dir, or provider env var |
| Budget | `--max-budget-usd` | collector-enforced | collector-enforced |
| Structured output | `--json-schema` | `--output-schema <file>` | [verify] |
| Resume | `--resume <sid>` (same `CLAUDE_CONFIG_DIR`) | `codex exec resume <sid>` (same `CODEX_HOME`) | `--session <sid>` (same data dir) |
| Cost | `total_cost_usd` (harness) | **not reported** → price table (`cost_source="estimated"`) | `step_finish.cost` (harness) |
| Model catalogue | none → aliases + known IDs | `codex debug models` (JSON) | `opencode models` |

Note on **autonomy without isolation**: `restricted` relies on the harness's own permission
system, which we explicitly do *not* trust as a security boundary. That is acceptable for layer 1
(developer testing on own machine) and is why `full` requires `unsafe=True` until a sandbox
exists. Security is layer 2's job.

---

## 7. Credentials

The core receives a `Credentials` value; it does not run logins or own a credential store
(that is a thin SDK helper — `phd auth login|status <harness>` — built on top).

```python
Credentials.claude_oauth_token(token)      # from `claude setup-token` — preferred (no refresh rotation)
Credentials.anthropic_api_key(key)
Credentials.codex_auth_file(path)          # ~/.codex/auth.json, copied into the run home
Credentials.openai_api_key(key)
Credentials.opencode_auth_file(path, providers=[...])  # only the needed provider entries
Credentials.*_from_env()                   # convenience
```

- Credentials go into `env` where possible; files only inside the run home, mode `0600`.
- Never written to `result.json`; `RunResult` records only the credential **kind**.
- Known risk: copied OAuth files (Codex `auth.json`, Claude `.credentials.json`) carry rotating
  refresh tokens — concurrent runs can invalidate each other or the host copy. Prefer
  long-lived tokens / API keys. [verify Codex behaviour]

---

## 8. Model validation

Before launch, `Spec.model` is checked against the harness catalogue:

- **codex**: `codex debug models` → JSON catalogue (slugs).
- **opencode**: `opencode models` → `provider/model` list.
- **claude**: no list command → known aliases (`opus`, `sonnet`, `haiku`, …) + known full IDs
  shipped with PHD, plus a `claude-*` pattern accepted with a warning; optional
  `validate="probe"` runs a 1-turn call.
- Catalogues cached per CLI version (TTL).

Failure → `ModelNotSupported(harness, model, supported=[...], hint=...)`, e.g.
*"`claude-haiku-5` is not a Claude model. Supported: opus, sonnet, haiku, claude-opus-5-5, …
Did you mean `claude-haiku-4-5`?"*

---

## 9. Neutral MCP format (`.mcp.json`)

Claude's `.mcp.json` schema is the neutral format:

```json
{ "mcpServers": {
    "taskboard": { "type": "http", "url": "http://host:8000/mcp",
                   "headers": { "Authorization": "Bearer ${env:TASKBOARD_TOKEN}" } },
    "files":     { "type": "stdio", "command": "uvx", "args": ["some-mcp"],
                   "env": { "ROOT": "${env:PROJECT_ROOT}" } } } }
```

`${env:NAME}` is resolved from `Spec.secrets`; an unresolved placeholder → `MissingSecret`.

---

## 10. Neutral hooks (decision B)

PHD defines a small event set and translates it per harness:

| Neutral event | Meaning | claude | codex | opencode |
|---|---|---|---|---|
| `session_start` | Run begins | `SessionStart` | [verify] | plugin `session.created` [verify] |
| `pre_tool` | Before a tool call; may **block** | `PreToolUse` | [verify] | plugin `tool.execute.before` |
| `post_tool` | After a tool call | `PostToolUse` | [verify] | plugin `tool.execute.after` |
| `stop` | Agent finishes | `Stop` | [verify] | plugin `session.idle` [verify] |

```json
{ "hooks": [
    { "event": "pre_tool", "match": { "tool": "shell" }, "command": "python hooks/guard.py", "timeout_s": 10 }
] }
```

- Hook command receives neutral JSON on stdin: `{event, run_id, cwd, tool, input, output?}`.
- Exit code `2` = block (with stderr as reason); `0` = allow.
- Neutral tool names: `shell`, `read`, `write`, `edit`, `web_fetch`, `web_search`,
  `mcp:<server>/<tool>` — mapped to/from each harness's tool names.
- A hook whose event cannot be mapped for the chosen harness → `HookUnsupported` at
  validation (fail loudly, never drop silently).

---

## 11. RunResult

| Group | Fields |
|-------|--------|
| Identity | `run_id`, `harness`, `model`, `cli_version`, `stack` (`name@version` + content hash), `started_at`, `finished_at` |
| Outcome | `status`: `succeeded \| failed \| timed_out \| budget_exceeded \| cancelled \| auth_failed`; `exit_code`; `error` (`{kind, message}`); `result_text`; `structured_output` |
| Cost | `cost_usd`, `cost_source`: `harness \| estimated \| unknown` |
| Usage | `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_tokens`, `num_turns`, `duration_ms` |
| Continuation | `session_id`, `resumable`, `run_dir` |
| Execution record | `cwd`, `project_config`, `autonomy`, `unsafe`, `credential_kind`, `sandbox: null` (layer 1) |
| Raw | `raw_final` (harness's final JSON), paths to `events.jsonl` and `stderr.log` |

### Run directory & ledger (decision B, sink later)

```
~/.phd/runs/<run_id>/
  home/          isolated config home (deleted after run if resumable=False)
  result.json    RunResult
  events.jsonl   normalized events (raw included)
  stderr.log
```

`phd.runs.list(...)`, `phd.runs.get(run_id)`. A `ResultSink` interface is defined now
(`async def put(result: RunResult)`); only the local ledger sink is implemented.

### Resume

`resume(result_or_run_id, msg=...)` re-launches with the same `run_dir/home` and the harness's
resume flag. Fails with `NotResumable(reason)` if the home was deleted, the harness differs, or
the session is missing. Cross-harness continuation is not supported (that is a handoff).

---

## 12. Error taxonomy

**Pre-launch (exceptions):** `UnknownHarness`, `HarnessNotInstalled(harness, hint)`,
`ModelNotSupported`, `CwdNotFound`, `EmptyTask`, `StackInvalid`, `StackIncompatible`,
`HookUnsupported`, `MissingSecret`, `MissingCredentials`, `UnsafeAutonomy`
(`autonomy="full"` without sandbox or `unsafe=True`), `NotResumable`.

**Post-launch (data):** `RunResult.status` + `error.kind` (`harness_crash`, `auth`,
`parse_error`, `timeout`, `budget`, `cancelled`). Auth failures are classified from the
harness output, not just stderr regex, where possible.

---

## 13. Testing

| Level | What | Cost |
|-------|------|------|
| L1 unit | Spec validation, prompt assembly, model-catalogue parsing, neutral→harness hook/MCP translation | Free |
| L2 golden | Materializer output per harness compared to committed golden trees | Free |
| L3 collector | Parsers fed recorded real stdout streams (success, error, timeout, resume) per harness and CLI version | Free |
| L4 live (opt-in, `PHD_LIVE=1`) | Same spec + stack on all 3 harnesses on the host; assert normalized `RunResult`; resume works; **hermeticity test** — a poisoned global config (hook that writes a marker file, global CLAUDE.md with a canary instruction) has **no effect** | Real money, small |

**Definition of done for layer 1:** L1–L3 green in CI; L4 green locally for all three harnesses.

---

## 14. Spikes required before implementation

Every **[verify]** above. Grouped:

1. **Claude** — how to suppress repo `CLAUDE.md` without `--bare`; `--append-system-prompt-file`
   form; whether appended system prompt persists across `--resume`; skills under
   `CLAUDE_CONFIG_DIR/skills` load in `-p` mode.
2. **Codex** — exact `--json` event names in 0.147 (usage, session id, final message); skills
   location; hooks config format and trust; suppressing repo AGENTS.md; API-key env var for
   `exec`; `auth.json` refresh behaviour when copied.
3. **OpenCode** — hermetic data/auth dir (XDG overrides); skills location; stdin prompt support;
   repo AGENTS.md suppression; structured output; plugin hook event names.
4. **Windows** — process-tree kill for `.cmd` shims (job objects); long prompts via stdin for all three.

---

## 15. Decisions log

**Layer 1 (this document):**
- Core = Spec + Stack + Materializer + Launcher + **Collector** (5th component, added).
- Core is location-agnostic; runs on the host with no isolation for now.
- Stacks are **harness-neutral** with an optional compatibility list.
- Stack contains identity (AGENTS.md); rules/brief/msg are per-run inputs.
- `project_config` = **C**: `ignore` (default) / `instructions_only` / `full`.
- Host user-level config never leaks into a run (hermetic config homes).
- Hooks = **B**: neutral event set, translated per harness (OpenCode via generic plugin).
- Results: `RunResult` + local ledger now, `ResultSink` interface for later.
- Python 3.12+, async-first, sync wrappers.
- Pre-launch errors raise; post-launch outcomes are data.

**Agreed for later layers (context, not in scope here):**
- Security comes from the sandbox, never the harness. Docker Engine API backend first
  (Podman-compatible); VM vs container still open.
- Sandboxes are named, persistent, reattachable, with idle policy; runs survive host disconnect.
- Network per sandbox: `harness_only` allowlist (egress proxy) by default, `open` opt-in.
- In-sandbox `phd-runner` (Approach 2) wraps this core; host SDK talks to it.
- Stacks: local (`~/.phd/local_stacks`, components in `~/.phd/{agents,skills,mcps,hooks}`) and
  remote (signed tarball, pulled into the sandbox, persisted on a volume; `stacks.list/pull/remove/prune`
  from the host). Each run's config home is built from the read-only stack.
- Git: clone into sandbox, agent commits on its own branch, PHD bundles out to `phd/<run_id>`; no
  push credentials inside.
- Skill confidentiality: impossible against the machine owner; stack authors choose delivery
  (plain/compiled, throwaway/persistent). Remote sandbox backend is the only fully confidential option.
- Inter-agent communication: out of scope for v1 (optional message-bus MCP plugin later).
- Desktop app: SDK needs Docker or Podman; a closed-source app may bundle Podman/WSL/Lima later.
