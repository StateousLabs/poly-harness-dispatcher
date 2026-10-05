# Errors

This page is the API reference for `PhdError` and its subclasses: the exceptions PHD raises *before* a harness
is launched. Causes and fixes: [Troubleshooting](../troubleshooting.md#pre-launch-errors-phderror).

Anything that goes wrong after launch is reported in `RunResult` (`status`, `error.kind`) and never raised.

## Summary

All classes are importable from `phd` and subclass `PhdError` (which subclasses `Exception`). Only
`ModelNotSupported` carries extra attributes.

| Exception | Raised when |
|---|---|
| `PhdError` | base class; catch it to handle every pre-launch problem |
| `UnknownHarness` | harness name is not `claude`, `codex` or `opencode` |
| `HarnessNotInstalled` | the CLI is not on `PATH` at launch or for the catalogue |
| `CwdNotFound` | `Spec.cwd` is not a directory |
| `InputNotFound` | `Inputs.brief` / `Inputs.rules` is not a file |
| `EmptyTask` | no brief and no non-blank msg |
| `StackInvalid` | malformed stack directory |
| `StackFetchError` | a stack source cannot be read or downloaded, or its archive is unusable or unsafe |
| `StackConflict` | the store has this `name@version` with different content (pass `replace=True`) |
| `StackNotFound` | no stored stack matches the reference, or a stack's folder no longer exists |
| `StackIncompatible` | stack's `harnesses` excludes `Spec.harness` |
| `HookUnsupported` | non-neutral hook event, or event/tool not expressible for the harness |
| `MissingSecret` | `${env:NAME}` without `Spec.secrets["NAME"]` |
| `MissingCredentials` | credentials missing, unreadable, for another harness, or of an unusable kind |
| `UnsafeAutonomy` | `autonomy="full"` without `unsafe=True` |
| `UnsupportedFeature` | feature unsupported by the harness (opencode `output_schema`); `Inputs.rules` on a resumed turn |
| `InvalidSpec` | invalid `Spec` field value |
| `InvalidRunsRoot` | codex `runs_root` under the system temp dir |
| `NotResumable` | a `session_id` cannot be continued (no run has it, other harness, home gone, other `cwd`) |
| `ModelNotSupported(harness, model, supported, hint=None)` | model not in the catalogue; attributes `harness`, `model`, `supported`, `hint` |

```python
from phd import Dispatcher, ModelNotSupported, PhdError

try:
    d = Dispatcher(spec)
except ModelNotSupported as e:
    print(e.hint, e.supported[:5])
except PhdError as e:
    print("cannot start:", e)
```

## Full reference

Rendered from the docstrings (MkDocs site only).

::: phd.PhdError

::: phd.UnknownHarness

::: phd.HarnessNotInstalled

::: phd.CwdNotFound

::: phd.InputNotFound

::: phd.EmptyTask

::: phd.StackInvalid

::: phd.StackFetchError

::: phd.StackConflict

::: phd.StackNotFound

::: phd.StackIncompatible

::: phd.HookUnsupported

::: phd.MissingSecret

::: phd.MissingCredentials

::: phd.UnsafeAutonomy

::: phd.UnsupportedFeature

::: phd.InvalidSpec

::: phd.InvalidRunsRoot

::: phd.NotResumable

::: phd.ModelNotSupported

---

**Next:** [Design record](../CoreDesign.md) · **See also:** [API overview](index.md), [Troubleshooting](../troubleshooting.md)
