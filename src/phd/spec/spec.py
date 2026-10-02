"""Spec (what/where/how) and Inputs (the task) (CoreDesign §2a)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..constants import AUTONOMY_MODES, HARNESSES, PROJECT_CONFIG_MODES
from ..errors import (CwdNotFound, EmptyTask, InputNotFound, InvalidSpec, MissingCredentials,
                      MissingSecret, UnknownHarness, UnsafeAutonomy)
from .credentials import Credentials
from .placeholders import env_refs
from .stack import Stack


@dataclass(frozen=True)
class Inputs:
    """The task for one run: a brief file, a message, and an optional rules file.

    Paths are expanded (`~`) and resolved to absolute paths at construction. The brief and the
    message become the first user message (on stdin); the rules file is appended to the system
    prompt. See `docs/concepts/prompt-assembly.md`.

    Attributes:
        brief: A task brief file of any name. Inlined as `# Task brief`.
        msg: A free-form message. Inlined as `# Message` after the brief.
        rules: A binding rules file. Appended to the system prompt as
            `## Project rules (binding)`.

    Raises:
        InputNotFound: `brief` or `rules` is given but is not a file.
        EmptyTask: Neither `brief` nor a non-blank `msg` is given.

    Example:
        ```python
        Inputs(brief="task.md", rules="RULES.md", msg="focus on the parser")
        Inputs(msg="List your skills.")
        ```
    """

    brief: Path | None = None
    msg: str | None = None
    rules: Path | None = None

    def __post_init__(self) -> None:
        for name in ("brief", "rules"):
            value = getattr(self, name)
            if value is not None:
                p = Path(value).expanduser().resolve()
                if not p.is_file():
                    raise InputNotFound(f"{name} file not found: {p}")
                object.__setattr__(self, name, p)
        if self.brief is None and not (self.msg and self.msg.strip()):
            raise EmptyTask("at least one of brief or msg is required")


@dataclass
class Spec:
    """What to run, where, and how: everything about a run except the task itself.

    All validation happens in the constructor, so a `Spec` that exists is internally consistent.
    `cwd` is resolved to an absolute path. Per-harness effects of each field are listed in
    `docs/concepts/spec-and-inputs.md` and `docs/concepts/combinations.md`.

    Attributes:
        harness: `"claude"`, `"codex"` or `"opencode"`.
        model: The model name passed to the harness (`--model` / `-m`). Checked against the
            harness catalogue by `Dispatcher` unless `validate_model=False`.
        stack: The agent: identity, skills, MCP servers and hooks (`Stack.load(...)`).
        cwd: The agent's working directory. Must exist.
        credentials: How the run authenticates; must be for the same harness.
        harness_config: Extra settings deep-merged into the harness's own config file
            (`settings.json` / `config.toml` / `opencode.json`). On a scalar conflict PHD's value
            wins and a warning is recorded.
        project_config: `"ignore"` (default; repo instructions and settings are not loaded),
            `"instructions_only"` (PHD appends the repo's `AGENTS.md` / `CLAUDE.md` to the
            system prompt) or `"full"` (the harness loads repo config natively).
        autonomy: `"restricted"` (default) or `"full"` (bypasses harness permission checks).
        unsafe: Must be `True` to use `autonomy="full"`; recorded in the result.
        secrets: Values for `${env:NAME}` placeholders in the stack's `.mcp.json` and hook
            commands. Excluded from `repr()`; scrubbed from the crash stderr tail.
        timeout_s: Wall-clock limit for the run, including writing the prompt. On expiry the
            process tree is killed and the result has status `timed_out`. Default 1800.
        budget_usd: Cost cap. claude gets `--max-budget-usd` (claude reports cost only in its
            final line, where PHD also checks it); opencode is stopped by PHD as soon as the
            summed `step_finish` cost exceeds it; codex reports no cost, so it is not enforced
            (a warning is added).
        output_schema: JSON Schema for a structured final answer (claude `--json-schema`, codex
            `--output-schema`). opencode raises `UnsupportedFeature`.
        resumable: Keep the run home after the run so `Dispatcher.resume` works. `False`
            deletes it once the result is built.
        harness_builtins: Keep the harness's own bundled skills and plugins. Default `False`
            suppresses them so the agent sees only its stack's skills.

    Raises:
        UnknownHarness: `harness` is not supported.
        InvalidSpec: Blank `model`; bad `project_config` / `autonomy`; non-bool
            `harness_builtins`; non-positive `timeout_s` or `budget_usd`.
        CwdNotFound: `cwd` is not a directory.
        UnsafeAutonomy: `autonomy="full"` without `unsafe=True`.
        MissingCredentials: `credentials.harness` differs from `harness`.
        StackIncompatible: The stack's `harnesses` list excludes `harness`.
        MissingSecret: The stack references `${env:NAME}` values absent from `secrets`.

    Example:
        ```python
        spec = Spec(harness="claude", model="sonnet", stack=Stack.load("config"),
                    cwd="path/to/repo", credentials=Credentials.from_host("claude"))
        ```
    """

    harness: str
    model: str
    stack: Stack
    cwd: Path
    credentials: Credentials
    harness_config: dict[str, Any] = field(default_factory=dict)
    project_config: str = "ignore"
    autonomy: str = "restricted"
    unsafe: bool = False
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    timeout_s: float = 1800.0
    budget_usd: float | None = None
    output_schema: dict | None = None
    resumable: bool = True
    harness_builtins: bool = False

    def __post_init__(self) -> None:
        if self.harness not in HARNESSES:
            raise UnknownHarness(f"unknown harness {self.harness!r}; expected one of {list(HARNESSES)}")
        if not isinstance(self.model, str) or not self.model.strip():
            raise InvalidSpec("model must be a non-empty string")
        self.cwd = Path(self.cwd).expanduser().resolve()
        if not self.cwd.is_dir():
            raise CwdNotFound(f"cwd is not a directory: {self.cwd}")
        if self.project_config not in PROJECT_CONFIG_MODES:
            raise InvalidSpec(f"project_config must be one of {list(PROJECT_CONFIG_MODES)}, got {self.project_config!r}")
        if self.autonomy not in AUTONOMY_MODES:
            raise InvalidSpec(f"autonomy must be one of {list(AUTONOMY_MODES)}, got {self.autonomy!r}")
        if self.autonomy == "full" and not self.unsafe:
            raise UnsafeAutonomy("autonomy='full' bypasses every harness permission check and PHD has no "
                                 "sandbox yet; pass unsafe=True to acknowledge")
        if self.credentials.harness != self.harness:
            raise MissingCredentials(f"credentials are for {self.credentials.harness!r}, spec harness is {self.harness!r}")
        if not isinstance(self.harness_builtins, bool):
            raise InvalidSpec(f"harness_builtins must be a bool, got {self.harness_builtins!r}")
        if self.timeout_s <= 0:
            raise InvalidSpec("timeout_s must be positive")
        if self.budget_usd is not None and self.budget_usd <= 0:
            raise InvalidSpec("budget_usd must be positive when set")
        self.stack.check_harness(self.harness)
        needed = env_refs(self.stack.mcp_servers) | env_refs([h.command for h in self.stack.hooks])
        missing = sorted(needed - set(self.secrets))
        if missing:
            raise MissingSecret(f"stack {self.stack.ref} needs secrets {missing}; pass them in Spec.secrets")
