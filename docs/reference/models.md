# Models

This page is the API reference for `ModelCatalog`, the pre-launch model check that `Dispatcher` runs by default.

## Summary

`ModelCatalog(runner=None)`: checks that a model name exists for a harness; loaded lists are cached per instance.

| Member | Purpose |
|---|---|
| `.models(harness) -> list[str]` | known model names (runs the catalogue command once per instance) |
| `.validate(harness, model) -> list[str]` | warnings if acceptable; raises `ModelNotSupported` if unknown |

Sources:

| Harness | Source | If the source fails |
|---|---|---|
| claude | built-in list: aliases `opus`, `sonnet`, `haiku`, `fable` + known ids; any other `claude-...` id passes with a warning; a `[1m]`-style suffix is ignored | n/a (no command) |
| codex | `codex debug models` (JSON, `slug` of each entry) | warning, model not checked |
| opencode | `opencode models` (lines with `/` and no space) | warning, model not checked |

A catalogue proves only that a model exists. A model the account cannot use fails after launch with
`error.kind == "model_unavailable"`.

```python
from phd import Dispatcher, ModelCatalog

catalog = ModelCatalog()
catalog.validate("claude", "claude-opus-5-5")   # []
Dispatcher(spec, catalog=catalog)               # reuse one catalogue (and its cache) across dispatchers
Dispatcher(spec, validate_model=False)          # skip the check
```

The `runner` argument (a function taking an argv list and returning stdout) lets tests inject fake catalogue
output.

## Full reference

Rendered from the docstrings (MkDocs site only).

::: phd.ModelCatalog

---

**Next:** [Errors](errors.md) · **See also:** [API overview](index.md), [Harnesses](../concepts/harnesses.md)
