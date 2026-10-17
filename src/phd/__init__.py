"""poly-harness-dispatcher: spawn headless coding agents from one harness-neutral spec."""

from . import runs, stacks
from .execution import Dispatcher, RunHandle
from .errors import (CwdNotFound, EmptyTask, HarnessNotInstalled, HookUnsupported, InputNotFound,
                     InvalidRunsRoot, InvalidSpec, MissingCredentials, MissingSecret, ModelNotSupported,
                     NotResumable, PhdError, StackConflict, StackFetchError, StackIncompatible,
                     StackInvalid, StackNotFound, UnknownHarness, UnsafeAutonomy, UnsupportedFeature)
from .harnesses import ModelCatalog
from .results import ERROR_KINDS, Event, LocalLedger, ResultSink, RunError, RunResult, RunStatus, Usage
from .spec import Credentials, HookDef, Inputs, Spec, Stack, StoreEntry

__version__ = "0.1.2"

__all__ = [
    "ERROR_KINDS", "Credentials", "CwdNotFound", "Dispatcher", "EmptyTask", "Event", "HarnessNotInstalled", "HookDef",
    "HookUnsupported", "InputNotFound", "Inputs", "InvalidRunsRoot", "InvalidSpec", "LocalLedger",
    "MissingCredentials", "MissingSecret", "ModelCatalog", "ModelNotSupported", "NotResumable", "PhdError",
    "ResultSink", "RunError", "RunHandle", "RunResult", "RunStatus", "Spec", "Stack", "StackConflict",
    "StackFetchError", "StackIncompatible", "StackInvalid", "StackNotFound", "StoreEntry", "UnknownHarness",
    "UnsafeAutonomy", "UnsupportedFeature", "Usage", "runs", "stacks",
]
