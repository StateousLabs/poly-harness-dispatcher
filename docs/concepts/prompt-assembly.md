# Prompt assembly

This page describes exactly what PHD puts into the system prompt and the first user message, in what order, how
`project_config` changes it, and what a resumed run sends.

The code is `src/phd/materialize/prompt.py` (`assemble`).

## Two channels

| Channel | Contents | Delivered via |
|---|---|---|
| **System prompt** | stack identity, then the rules file, then (only for `instructions_only`) repo instruction files | a file in the run home: claude `system-prompt.md` (`--append-system-prompt-file`), codex `AGENTS.md` (`$CODEX_HOME`), opencode `AGENTS.md` (`instructions`) |
| **First user message** | brief, then msg | the harness process's **stdin** (codex gets `-` as its prompt argument) |

Writing the user message to stdin avoids the Windows command-line limits and `cmd.exe` mangling of `<`, `>` and
newlines. It is written in the background, so a harness that never reads stdin cannot block past `timeout_s`.

## System prompt

Blocks are joined with a blank line; the result ends with a newline. Each file's content is stripped of leading
and trailing whitespace.

| # | Block | Present when |
|---|---|---|
| 1 | the stack's `agents/<one>.md` text | always |
| 2 | `## Project rules (binding)` + blank line + `Inputs.rules` file | `Inputs.rules` is set |
| 3 | `## Project context (AGENTS.md)` + blank line + `cwd/AGENTS.md` | `project_config="instructions_only"` and the file exists |
| 4 | `## Project context (CLAUDE.md)` + blank line + `cwd/CLAUDE.md` | `project_config="instructions_only"` and the file exists |

Only files directly in `Spec.cwd` are read for blocks 3 and 4 (not parent directories, not `.claude/`).

## First user message

| # | Block | Present when |
|---|---|---|
| 1 | `# Task brief` + blank line + `Inputs.brief` file | `Inputs.brief` is set |
| 2 | `# Message` + blank line + `Inputs.msg` (stripped) | `Inputs.msg` is non-blank |

The brief is inlined, so the agent cannot skip reading it.

Example: `Inputs(brief="task.md", rules="RULES.md", msg="focus on tests")` with `project_config="ignore"`:

```text
--- system prompt (home/system-prompt.md, home/AGENTS.md) ---
# Identity: Helper
...identity text...

## Project rules (binding)

...RULES.md...

--- stdin ---
# Task brief

...task.md...

# Message

focus on tests
```

## Effect of `project_config`

| Mode | PHD adds repo files to the system prompt | Harness loads repo config itself |
|---|---|---|
| `ignore` | no | no (suppressed; see [Harnesses](harnesses.md)) |
| `instructions_only` | yes: `cwd/AGENTS.md`, then `cwd/CLAUDE.md` | no |
| `full` | no | yes, natively (claude `--setting-sources user,project,local`; codex reads repo `AGENTS.md`; opencode project config) |

With `full`, the repo's instructions are not duplicated by PHD; how the harness combines them with the identity
is the harness's behaviour.

## Resumed turns

A turn started with `session_id=` builds its first user message from `Inputs` exactly as a fresh run does
(`# Task brief` and/or `# Message`; `EmptyTask` if both are missing). `Inputs.rules` is rejected with
`UnsupportedFeature`: rules belong to the system prompt, which the session fixed when it started (claude snapshots
it at session start).

The earlier conversation comes from the harness session in the shared run home. The system-prompt file
(claude `system-prompt.md`, codex and opencode `AGENTS.md`) is rewritten from the resuming `Spec` without rules:
the identity and, with `project_config="instructions_only"`, the project context. Claude's argv re-passes
`--append-system-prompt-file`; codex re-reads `$CODEX_HOME/AGENTS.md`; opencode re-reads `instructions` from its
`opencode.json`. How much of a rewritten file a harness picks up mid-session is harness behaviour; the rules the
session started with stay in its conversation (CoreDesign §14.2 verified identity and rules on a resumed turn for
all three harnesses).

## Inspecting it

```python
plan = Dispatcher(spec).plan(inputs)
print(plan.stdin)                                                   # first user message
print((plan.home / "system-prompt.md").read_text(encoding="utf-8"))  # claude; codex/opencode: AGENTS.md
```

---

**Next:** [Hooks](hooks.md) · **See also:** [Spec: project_config](spec-and-inputs.md#project_config), [Run lifecycle](run-lifecycle.md)
