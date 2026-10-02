"""Pre-launch errors. Anything that goes wrong *after* a harness starts is reported as data in
RunResult, never raised (CoreDesign §12)."""


class PhdError(Exception):
    """Base class for every error PHD raises before launching a harness.

    Catch `PhdError` to handle every pre-launch problem; the message names the offending file,
    field or value.
    """


class UnknownHarness(PhdError):
    """The harness name is not `claude`, `codex` or `opencode`."""


class HarnessNotInstalled(PhdError):
    """The harness CLI was not found on PATH at launch (or by `ModelCatalog`)."""


class CwdNotFound(PhdError):
    """`Spec.cwd` is not an existing directory."""


class InputNotFound(PhdError):
    """`Inputs.brief` or `Inputs.rules` is not an existing file."""


class EmptyTask(PhdError):
    """`Inputs` has neither a brief nor a non-blank msg, or a resume msg is blank."""


class StackInvalid(PhdError):
    """The stack directory is malformed (`stack.json`, `agents/`, `skills/`, `.mcp.json`, `hooks.json`)."""


class StackIncompatible(PhdError):
    """The stack's `harnesses` list does not include `Spec.harness`."""


class HookUnsupported(PhdError):
    """A hook's event is not neutral, or its event/tool cannot be expressed for the harness."""


class MissingSecret(PhdError):
    """The stack references `${env:NAME}` but `Spec.secrets` has no `NAME`."""


class MissingCredentials(PhdError):
    """Credentials are missing, unreadable, for another harness, or of a kind the harness cannot use."""


class UnsafeAutonomy(PhdError):
    """`autonomy="full"` was requested without `unsafe=True`."""


class UnsupportedFeature(PhdError):
    """The spec uses a feature the harness does not support (opencode `output_schema`)."""


class InvalidSpec(PhdError):
    """A `Spec` field has an invalid value (model, modes, `harness_builtins`, `timeout_s`, `budget_usd`)."""


class InvalidRunsRoot(PhdError):
    """codex with a `runs_root` under the system temp dir (codex refuses a temp `CODEX_HOME`)."""


class NotResumable(PhdError):
    """A resume target cannot be continued: unknown run, other harness, no session, or no home."""


class ModelNotSupported(PhdError):
    """`Spec.model` is not in the harness's model catalogue.

    Attributes:
        harness: The harness name.
        model: The rejected model.
        supported: The catalogue the model was checked against.
        hint: The closest catalogue entry, if any.
    """

    def __init__(self, harness: str, model: str, supported: list[str], hint: str | None = None):
        self.harness, self.model, self.supported, self.hint = harness, model, supported, hint
        msg = f"{model!r} is not a {harness} model."
        if hint:
            msg += f" Did you mean {hint!r}?"
        if supported:
            msg += " Supported: " + ", ".join(supported[:25]) + (" …" if len(supported) > 25 else "")
        super().__init__(msg)
