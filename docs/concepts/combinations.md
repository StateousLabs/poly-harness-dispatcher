# Option combinations

This page collects, as matrices, what each harness does for each combination of options: `project_config` ×
`autonomy`, `harness_builtins`, budget enforcement, `output_schema`, `resumable` × resume, credential kinds, and
which errors are raised before launch versus returned as data.

## harness × project_config × autonomy

Every combination is accepted; the only rule is `autonomy="full"` requires `unsafe=True` (`UnsafeAutonomy`).
Cells show what PHD sets; what the harness then permits is the harness's own behaviour.

| Harness | `project_config` | `autonomy="restricted"` | `autonomy="full"`, `unsafe=True` |
|---|---|---|---|
| claude | `ignore` | `--setting-sources user --permission-mode acceptEdits` | `--setting-sources user --dangerously-skip-permissions` |
| claude | `instructions_only` | same as `ignore` + repo `AGENTS.md`/`CLAUDE.md` in the system prompt | same as `ignore` + repo files in the system prompt |
| claude | `full` | `--setting-sources user,project,local --permission-mode acceptEdits` | `--setting-sources user,project,local --dangerously-skip-permissions` |
| codex | `ignore` | `sandbox_mode="workspace-write"`, `project_doc_max_bytes=0`, `--ignore-rules`, repo `.agents/skills` disabled | `sandbox_mode="danger-full-access"` + the same suppression |
| codex | `instructions_only` | as `ignore` + repo files in `AGENTS.md` | as `ignore` + repo files |
| codex | `full` | `sandbox_mode="workspace-write"`, no repo-level suppression (the user's `~/.agents/skills` stay disabled) | `sandbox_mode="danger-full-access"`, no repo-level suppression (user skills stay disabled) |
| opencode | `ignore` | `OPENCODE_DISABLE_PROJECT_CONFIG=1`; permission `edit: allow, bash: {"*": deny}, external_directory: deny` | `OPENCODE_DISABLE_PROJECT_CONFIG=1`; permission `{"*": allow}`; `--auto` |
| opencode | `instructions_only` | as `ignore` + repo files in `AGENTS.md` | as `ignore` + repo files |
| opencode | `full` | no `OPENCODE_DISABLE_PROJECT_CONFIG`; restricted permission | no `OPENCODE_DISABLE_PROJECT_CONFIG`; `{"*": allow}`, `--auto` |

codex always gets `approval_policy = "never"`. What that means in practice under `restricted`:

| Harness | File edits | Shell commands | Outside `cwd` |
|---|---|---|---|
| claude | auto-accepted (`acceptEdits`) | need an allow rule via `harness_config` (for example `Bash(echo:*)`; on Windows also `PowerShell(...)`) | no extra deny rules from PHD (CoreDesign's "deny rules outside cwd" is not implemented) |
| codex | inside the codex `workspace-write` sandbox | run without approval prompts, inside that sandbox | governed by codex's sandbox |
| opencode | allowed | denied unless allowed via `harness_config` (for example `{"permission": {"bash": {"echo *": "allow"}}}`) | `external_directory: deny` |

`project_config="full"` lets repo hooks, settings and MCP servers run: that is code PHD does not control.

## harness_builtins

| Harness | `False` (default) | `True` |
|---|---|---|
| claude | bundled skills off, 3 builtin plugins off, `Skill(design)` / `Skill(doctor)` denied | Claude Code defaults |
| codex | `[skills.bundled] enabled = false` | codex defaults |
| opencode | `permission.skill.customize-opencode = "deny"` | opencode defaults |

Independent of `harness_builtins`: the host's user-level skills never load (claude/opencode: separate config home
and env flags; codex: the user's `~/.agents/skills/*/SKILL.md` disabled in `config.toml` for every `project_config`, repo-level `.agents/skills` too unless `project_config="full"`).

## Budget enforcement

| Harness | Mechanism | When it stops | Resulting status |
|---|---|---|---|
| claude | `--max-budget-usd <budget>` passed to claude (a per-invocation cap), **and** PHD compares this turn's own cost with the budget (on a resumed turn: claude's cumulative figure minus the session's earlier cost) | claude applies its own cap; PHD can only check at the final `result` line, where claude reports cost | `budget_exceeded` (claude's own error subtype contains `budget`, or PHD saw cost > budget) |
| codex | none: codex reports no cost | never | `budget_usd` is ignored; `Dispatcher(...)` adds the warning `codex reports no cost, so budget_usd cannot be enforced for this run` |
| opencode | PHD sums `step_finish.cost` and kills the process tree when the sum exceeds the budget | after the step that crossed it | `budget_exceeded`; `cost_usd` is the sum at that point |

Enforcement is after the fact: the step that crosses the budget has already been paid for.

## output_schema

A malformed schema raises `InvalidSpec` at `Spec(...)` (checked with `jsonschema`; the draft comes from the
schema's `$schema`, default 2020-12).

| Harness | How the schema reaches the model | `structured_output` comes from |
|---|---|---|
| claude | `--json-schema '<schema JSON>'` | the `structured_output` key of claude's `result` line; else `result_text` parsed as JSON |
| codex | the strict-mode converted schema (see below) written to `home/output-schema.json`, `--output-schema <path>` (also on resume, from the resuming `Spec`) | `result_text` parsed as JSON |
| opencode | an instruction appended to the system prompt (`AGENTS.md`, section `## Final answer format`) asking for only a JSON value matching the schema. OpenCode's native `format: json_schema` exists only in its SDK/server API, not in `opencode run` | `result_text` parsed as JSON |

### Per-harness schema translation

Your `output_schema` is used as given; PHD adapts it for the harness and changes the answer back, so
`RunResult.structured_output` is always in **your** schema's shape and PHD validates it against **your original
schema**.

- **Wrapper.** Claude and Codex refuse a schema whose root is not an object (a root `oneOf` / `anyOf` / `allOf` /
  `$ref`, or a non-object type). For such a schema PHD sends
  `{"type": "object", "properties": {"outcome": <your schema>}, "required": ["outcome"], "additionalProperties": false}`
  (your `$schema`, `$defs` and `definitions` move to the wrapper root so local `#/$defs/...` refs keep working; a ref
  into `#/properties/...` is not supported on wrapped schemas) and takes `outcome` out of the answer. A schema whose
  root is already an object is sent as given (claude), so existing callers see no change.
- **Codex strict mode.** Codex (OpenAI strict structured output) is sent a converted copy: `oneOf` becomes `anyOf`;
  every property of every object is listed in `required`, and a property that was **not** required in your schema
  becomes nullable (`{"anyOf": [<schema>, {"type": "null"}]}`); `additionalProperties: false` is added on every object
  where you omitted it (an object that has `properties`). `$schema` is dropped from the written file. In the answer PHD
  removes a `null` for a property that was optional in your schema, but keeps a `null` your schema allows explicitly
  (for example `"type": ["string", "null"]`).
- **Refused for codex.** Strict mode cannot express these, so `Dispatcher(spec)` raises `UnsupportedFeature` naming the
  keyword or the path (nothing is dropped silently): a free-form object (an object schema without `properties`, also
  `"type": ["object", "null"]`); `additionalProperties` set to `true` or to a schema; `allOf`; a node with both `oneOf`
  and `anyOf`; list-form `items` and `additionalItems`; `dependencies`, `dependentSchemas`, `dependentRequired`;
  `patternProperties`, `propertyNames`, `contains`; `if` / `then` / `else`, `not`; `unevaluatedProperties`,
  `unevaluatedItems`. Claude and OpenCode accept any valid schema.
- **OpenCode** is unchanged: the schema is an instruction in `AGENTS.md`.
- Resume and the repair turn use the same translated file; the repair prompt quotes the error from validating against
  your schema.

**PHD validates the answer on every harness**, whatever the native mechanism, after the run's own error checks:

- The answer is the harness's structured output if it gave one, else `result_text` parsed as JSON. Surrounding text
  is not tolerated, but a single fenced code block (```` ```json ````) is.
- Not JSON: `error.kind == "schema"`, message `final answer is not JSON`.
- JSON that fails the schema: `kind == "schema"`, message `<json path>: <message>` (for example
  `$.answer: 'x' is not of type 'integer'`), with secret values (`Spec.secrets`, the credential secret) scrubbed first
  and then capped at 500 characters.
- `RunResult.structured_output` keeps the parsed value even when it fails validation, so you can inspect it. It is
  `None` only when no JSON could be extracted.
- A `schema` error applies only when the run had no other error (it is the last rank in
  [the precedence list](results-and-events.md#error-kinds-and-precedence)); status is `failed`.
- With `Spec.schema_repair=True` PHD first tries one repair turn: see [Run lifecycle](run-lifecycle.md#schema-repair-turn).

## resumable × resume

Resuming means `start(inputs, session_id=result.session_id)` / `spawn(..., session_id=...)`.

| Run | `Spec.resumable` | Session id reported | `RunResult.resumable` | Home after the run | Resuming its session |
|---|---|---|---|---|---|
| fresh | `True` (default) | yes | `True` | kept | works |
| fresh | `True` | no (for example crash before init) | `False` | kept | nothing to resume (`session_id` is `None`) |
| fresh | `False` | any | `False` | deleted (a failure to delete becomes a warning) | a failed run with `error.kind == "session"` ("no longer exists"): the chain ends |
| resumed turn | any | yes | `True` | never deleted by a resumed turn | works (same shared home) |
| any | any | yes, but the run failed | as above | as above | works: status does not matter, only the session id |

Resume also requires the same harness and `cwd`, and rejects `Inputs.rules` (`UnsupportedFeature`); all causes:
[Run lifecycle](run-lifecycle.md#resume).

## Credential kind × harness

| Kind | claude | codex | opencode |
|---|---|---|---|
| `claude_oauth_token` | env `CLAUDE_CODE_OAUTH_TOKEN` | n/a | n/a |
| `anthropic_api_key` | env `ANTHROPIC_API_KEY` | n/a | n/a |
| `claude_host_login` | file `home/.credentials.json` | n/a | n/a |
| `codex_auth_file` | n/a | file `home/auth.json` | n/a |
| `opencode_auth_file` | n/a | n/a | env `OPENCODE_AUTH_CONTENT` |

"n/a": the constructors fix the harness, and `Spec` rejects credentials for another harness
(`MissingCredentials`). A hand-built `Credentials` with an unknown kind fails in the adapter with
`MissingCredentials` at materialize time.

## Raised before launch vs returned as data

| Where | Raised (`PhdError` unless noted) |
|---|---|
| `Stack.load(path)` | `StackInvalid`, `HookUnsupported` |
| `Credentials.*(...)` | `MissingCredentials` (file missing), `UnknownHarness` (`from_host`) |
| `Inputs(...)` | `InputNotFound`, `EmptyTask` |
| `Spec(...)` | `UnknownHarness`, `InvalidSpec`, `CwdNotFound`, `UnsafeAutonomy`, `MissingCredentials`, `StackIncompatible`, `MissingSecret` |
| `Dispatcher(spec, ...)` | `HookUnsupported`, `InvalidRunsRoot`; `ModelNotSupported` for claude only |
| `plan()` / `start()` / `spawn()` | `ModelNotSupported` (codex: the catalogue is checked here, not in `Dispatcher(...)`; an opencode miss is a warning), `MissingCredentials` (credential file unreadable), `MissingSecret`; `start`/`spawn` also `HarnessNotInstalled`; `spawn` inside an event loop: `RuntimeError` |
| the same with `session_id=` | additionally `NotResumable` (session busy, tampered home) and `UnsupportedFeature` (`Inputs.rules` given) |
| `ModelCatalog.models()` | `UnknownHarness`, `HarnessNotInstalled`, `RuntimeError` |

Returned as data (`RunResult.status` / `error.kind`), never raised: `cancelled`, `timeout`, `idle`, `schema`, `budget`,
`stream_overflow`, `phd_internal`, `session`, `auth`, `model_unavailable`, `billing`, `harness_error`, `harness_crash`,
`no_result`.

Not wrapped in `PhdError` today (CoreDesign §14.2, open item 12): filesystem errors while planning (`OSError`),
an undecodable brief, rules or identity file (`UnicodeDecodeError`), and `timeout_s=float("nan")`.

---

**Next:** [Security and isolation](security-and-isolation.md) · **See also:** [Spec, Inputs, Credentials](spec-and-inputs.md), [Harnesses](harnesses.md)
