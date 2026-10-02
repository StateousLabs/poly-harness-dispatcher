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
| codex | `full` | `sandbox_mode="workspace-write"`, no suppression | `sandbox_mode="danger-full-access"`, no suppression |
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
and env flags; codex: foreign `SKILL.md` files disabled in `config.toml`).

## Budget enforcement

| Harness | Mechanism | When it stops | Resulting status |
|---|---|---|---|
| claude | `--max-budget-usd <budget>` passed to claude, **and** PHD compares claude's reported cost with the budget | claude applies its own cap; PHD can only check at the final `result` line, where claude reports cost | `budget_exceeded` (claude's own error subtype contains `budget`, or PHD saw cost > budget) |
| codex | none: codex reports no cost | never | `budget_usd` is ignored; `Dispatcher(...)` adds the warning `codex reports no cost, so budget_usd cannot be enforced for this run` |
| opencode | PHD sums `step_finish.cost` and kills the process tree when the sum exceeds the budget | after the step that crossed it | `budget_exceeded`; `cost_usd` is the sum at that point |

Enforcement is after the fact: the step that crosses the budget has already been paid for.

## output_schema

| Harness | Mechanism | `structured_output` comes from |
|---|---|---|
| claude | `--json-schema '<schema JSON>'` | the `structured_output` key of claude's `result` line; else `result_text` parsed as JSON |
| codex | schema written to `home/output-schema.json`, `--output-schema <path>` (kept on resume) | `result_text` parsed as JSON (left `None` if not JSON) |
| opencode | not supported | `UnsupportedFeature` at `Dispatcher(...)` |

PHD does not validate the answer against the schema.

## resumable × resume

| `Spec.resumable` | Session id reported | `RunResult.resumable` | Home after the run | `resume(...)` |
|---|---|---|---|---|
| `True` (default) | yes | `True` | kept | works |
| `True` | no (for example crash before init) | `False` | kept | `NotResumable` ("has no session id") |
| `False` | any | `False` | deleted (a failure to delete becomes a warning) | `NotResumable` ("no longer exists") |
| any, on a resumed run | yes | `Spec.resumable` | never deleted by a resumed run | works (resumes the same home) |

Resume is same-harness only; other `NotResumable` causes: [Run lifecycle](run-lifecycle.md#resume).

## Credential kind × harness

| Kind | claude | codex | opencode |
|---|---|---|---|
| `claude_oauth_token` | env `CLAUDE_CODE_OAUTH_TOKEN` | n/a | n/a |
| `anthropic_api_key` | env `ANTHROPIC_API_KEY` | n/a | n/a |
| `claude_host_login` | env `CLAUDE_CODE_OAUTH_TOKEN` (access token read from the file) | n/a | n/a |
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
| `Dispatcher(spec, ...)` | `HookUnsupported`, `UnsupportedFeature`, `ModelNotSupported`, `InvalidRunsRoot` |
| `plan()` / `start()` / `spawn()` | `MissingCredentials` (credential file unreadable), `MissingSecret`; `start`/`spawn` also `HarnessNotInstalled`; `spawn` inside an event loop: `RuntimeError` |
| `start_resume()` / `resume()` | `NotResumable`, `EmptyTask`, `MissingCredentials`, `HarnessNotInstalled`; `resume` inside an event loop: `RuntimeError` |
| `ModelCatalog.models()` | `UnknownHarness`, `HarnessNotInstalled`, `RuntimeError` |

Returned as data (`RunResult.status` / `error.kind`), never raised: `cancelled`, `timeout`, `budget`,
`stream_overflow`, `phd_internal`, `auth`, `model_unavailable`, `billing`, `harness_error`, `harness_crash`,
`no_result`.

Not wrapped in `PhdError` today (CoreDesign §14.2, open item 12): filesystem errors while planning (`OSError`),
an undecodable brief, rules or identity file (`UnicodeDecodeError`), and `timeout_s=float("nan")`.

---

**Next:** [Security and isolation](security-and-isolation.md) · **See also:** [Spec, Inputs, Credentials](spec-and-inputs.md), [Harnesses](harnesses.md)
