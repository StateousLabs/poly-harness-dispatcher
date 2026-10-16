# File formats

Copy-paste examples of every file PHD reads, so you can see the shape before you read the rules. Each example is
loaded by the real validators in the test suite's docs check, so none of them is invalid. The rules and error
messages live on the linked concept pages.

| File | Where | Required | Rules |
|---|---|---|---|
| `stack.json` | stack root | yes | [Stacks](../concepts/stacks.md#stackjson) |
| `agents/<one>.md` | stack | yes, exactly one | [Stacks](../concepts/stacks.md#agentsonemd-the-identity) |
| `skills/<name>/SKILL.md` | stack | no | [Stacks](../concepts/stacks.md#skills) |
| `.mcp.json` | stack root | no | [Stacks](../concepts/stacks.md#mcpjson-mcp-servers) |
| `hooks/hooks.json` | stack | no | [Stacks](../concepts/stacks.md#hookshooksjson-neutral-hooks), [Hooks](../concepts/hooks.md) |
| `stack.toml` | anywhere | no | [Stack resolver](../concepts/stack-resolver.md#the-manifest) |
| `output_schema` | `Spec` | no | [Option combinations](../concepts/combinations.md#output_schema) |

## The smallest stack

Two files are enough: a `stack.json` and one agent file.

```
minimal/
  stack.json
  agents/helper.md
```

```json title="minimal/stack.json"
{
  "name": "minimal",
  "version": "0.1.0"
}
```

```markdown title="minimal/agents/helper.md"
# Helper

You are a careful assistant. Keep answers short and factual.
```

```python
from phd import Stack
stack = Stack.load("minimal")
```

## A full stack

One identity, a skill, an MCP server and a hook:

```
reviewer/
  stack.json
  agents/reviewer.md
  skills/pytest-notes/
    SKILL.md
    references/notes.md
  .mcp.json
  hooks/
    hooks.json
    guard.py
  mcp/echo_server.py          anything else is copied with the stack
```

### `stack.json`

Only `name` and `version` are required. `harnesses` restricts which harnesses may run the stack, and
`default_models` is stored but never read by PHD (`Spec.model` is always required).

```json title="reviewer/stack.json"
{
  "name": "reviewer",
  "version": "1.0.0",
  "description": "Reviews pull requests against the team's testing rules.",
  "harnesses": ["claude", "codex", "opencode"],
  "default_models": { "claude": "sonnet" }
}
```

### `agents/<one>.md`

The whole file is the agent's identity. It becomes the first block of the system prompt. Front matter is not
parsed, so write plain Markdown.

```markdown title="reviewer/agents/reviewer.md"
# Identity: Reviewer

You review pull requests for correctness and test quality.

- Read the diff before you comment.
- Cite file and line for every finding.
- End your final answer with a one-line verdict.
```

### `skills/<name>/SKILL.md`

Each folder under `skills/` is a skill and must contain `SKILL.md`. PHD does not parse it; the front matter
(`name`, `description`) is read by the harness, so follow your harness's skill format. The whole folder is copied,
including `references/` and `scripts/`.

```markdown title="reviewer/skills/pytest-notes/SKILL.md"
---
name: pytest-notes
description: Use when reviewing Python tests. Reads references/notes.md for the team's pytest conventions.
---

# Pytest notes

Read `references/notes.md`, then check the diff against it.
```

```markdown title="reviewer/skills/pytest-notes/references/notes.md"
- One behaviour per test.
- No sleeps; wait on a condition.
```

### `.mcp.json`

Claude's `.mcp.json` schema. A server is `stdio` by default (`command`, optional `args` and `env`) or `http`
(`url`, optional `headers`). `${stack}` is the run's copy of the stack; `${env:NAME}` comes from `Spec.secrets`,
never from the host environment.

```json title="reviewer/.mcp.json"
{
  "mcpServers": {
    "echo": {
      "type": "stdio",
      "command": "python",
      "args": ["${stack}/mcp/echo_server.py"],
      "env": { "ECHO_PREFIX": "${env:ECHO_PREFIX}" }
    },
    "docs": {
      "type": "http",
      "url": "https://docs.example.com/mcp",
      "headers": { "Authorization": "Bearer ${env:DOCS_TOKEN}" }
    }
  }
}
```

A stack with `${env:…}` placeholders needs those names in `Spec.secrets`, or `Spec(...)` raises `MissingSecret`.

### `hooks/hooks.json`

Neutral hooks. `event` is one of `session_start`, `pre_tool`, `post_tool`, `stop`; `match.tool` is a neutral tool
name; `timeout_s` defaults to 30. The command receives a JSON payload on stdin
([payload and exit codes](../concepts/hooks.md#payload-your-command-receives-stdin)).

```json title="reviewer/hooks/hooks.json"
{
  "hooks": [
    {
      "event": "pre_tool",
      "match": { "tool": "shell" },
      "command": "python ${stack}/hooks/guard.py",
      "timeout_s": 10
    }
  ]
}
```

## Composing a stack: `stack.toml`

A manifest builds a stack from a base plus components, each from a local path or an `https://` URL. Relative
sources resolve against the manifest's own directory. Resolve it with `phd.stacks.resolve("compose")`.

```
compose/
  stack.toml
  extra/lint-notes/SKILL.md
```

```toml title="compose/stack.toml"
name = "reviewer-plus"
version = "1.1.0"
description = "The reviewer stack plus one extra skill."
harnesses = ["claude", "codex"]

base = "../reviewer"

[[skills]]
source = "./extra/lint-notes"
```

```markdown title="compose/extra/lint-notes/SKILL.md"
---
name: lint-notes
description: Use when asked about lint rules.
---

# Lint notes

Prefer fixing the code over silencing the rule.
```

URL sources, private-registry `headers` with `${env:…}`, `replace = true` and archives are covered in the
[stack resolver](../concepts/stack-resolver.md#the-manifest).

## A structured-output schema

`Spec.output_schema` is a plain JSON Schema dict (any draft, 2020-12 by default; `$ref`s must resolve locally).
The run's answer is validated against it, and a mismatch gives error kind `schema`.

```python
from phd import Spec, Stack

verdict = {
    "type": "object",
    "properties": {
        "verdict": {"enum": ["approve", "request_changes"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "note": {"type": "string"},
                },
                "required": ["file", "note"],
            },
        },
    },
    "required": ["verdict"],
}

spec = Spec(harness="claude", model="sonnet", stack=Stack.load("reviewer"),
            cwd="repo", output_schema=verdict)
# after the run: result.structured_output == {"verdict": "approve", "findings": []}
```

You always write and receive the answer in your own schema's shape. PHD adapts a copy for each harness (a wrapper
for a non-object root, strict mode for codex) and changes the answer back, as described under
[per-harness schema translation](../concepts/combinations.md#per-harness-schema-translation).
