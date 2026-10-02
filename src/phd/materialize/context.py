"""The materialize context and the materialized home: what an adapter is handed and what it yields."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .._util.fs import deep_merge
from .._util.shell import shell_join
from ..constants import REDACTED
from ..spec import Spec
from ..spec.placeholders import resolve
from .prompt import Prompt

SECRET_ENV = re.compile(r"TOKEN|KEY|AUTH_CONTENT|SECRET|PASSWORD", re.IGNORECASE)


@dataclass
class MaterializeContext:
    spec: Spec
    prompt: Prompt
    run_id: str
    run_dir: Path
    home: Path
    stack_dir: Path
    warnings: list[str] = field(default_factory=list)

    def mcp_servers(self) -> dict:
        return resolve(self.spec.stack.mcp_servers, stack_dir=self.stack_dir.as_posix(), secrets=self.spec.secrets)

    def hooks(self) -> list[dict]:
        # hook commands are shell lines: quote the stack path so a spaced run dir stays one argument
        stack = shell_join([self.stack_dir.as_posix()])
        return [resolve(h.as_dict(), stack_dir=stack, secrets=self.spec.secrets) for h in self.spec.stack.hooks]

    def merge_harness_config(self, base: dict) -> dict:
        conflicts: list[str] = []
        merged = deep_merge(base, self.spec.harness_config, conflicts=conflicts)
        for key in conflicts:
            self.warnings.append(f"harness_config key {key!r} conflicts with a PHD-owned value; PHD's value was kept")
        return merged

    def copy_skills(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        for skill in self.spec.stack.skills:
            shutil.copytree(skill, dest / skill.name, dirs_exist_ok=True)


@dataclass
class MaterializedHome:
    """A written run home, ready to launch (returned by `Dispatcher.plan`).

    Attributes:
        run_id: The allocated run id.
        run_dir: `<runs_root>/<run_id>`.
        home: `<run_dir>/home`, the harness's isolated config home.
        argv: The harness command line (argv[0] is the bare CLI name, resolved on PATH at launch).
        env: Variables PHD sets for the child (config home, credentials); excluded from `repr()`.
        stdin: The first user message.
        warnings: Pre-launch and materialization warnings.
        rules_text: The rules block in the system prompt (stripped rules file text), or `None`.
    """

    run_id: str
    run_dir: Path
    home: Path
    argv: list[str]
    env: dict[str, str] = field(repr=False)
    stdin: str
    warnings: list[str]
    rules_text: str | None = None

    def redacted_env(self) -> dict[str, str]:
        """`env` with values of names matching TOKEN, KEY, AUTH_CONTENT, SECRET or PASSWORD replaced by `<redacted>`."""
        return {k: (REDACTED if SECRET_ENV.search(k) else v) for k, v in self.env.items()}
