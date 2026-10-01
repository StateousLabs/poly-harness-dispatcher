"""poly-harness-dispatcher: spawn headless coding agents from one harness-neutral spec."""

from . import runs
from .dispatcher import Dispatcher, RunHandle
from .errors import (CwdNotFound, EmptyTask, HarnessNotInstalled, HookUnsupported, InputNotFound,
                     InvalidRunsRoot, InvalidSpec, MissingCredentials, MissingSecret, ModelNotSupported,
                     NotResumable, PhdError, StackIncompatible, StackInvalid, UnknownHarness,
                     UnsafeAutonomy, UnsupportedFeature)
from .harnesses.catalog import ModelCatalog
from .results import Event, LocalLedger, ResultSink, RunError, RunResult, RunStatus, Usage
from .spec import Credentials, HookDef, Inputs, Spec, Stack

__version__ = "0.1.0"

__all__ = [
    "Credentials", "CwdNotFound", "Dispatcher", "EmptyTask", "Event", "HarnessNotInstalled", "HookDef",
    "HookUnsupported", "InputNotFound", "Inputs", "InvalidRunsRoot", "InvalidSpec", "LocalLedger",
    "MissingCredentials", "MissingSecret", "ModelCatalog", "ModelNotSupported", "NotResumable", "PhdError",
    "ResultSink", "RunError", "RunHandle", "RunResult", "RunStatus", "Spec", "Stack", "StackIncompatible",
    "StackInvalid", "UnknownHarness", "UnsafeAutonomy", "UnsupportedFeature", "Usage", "runs",
]
