"""Shared adapter plumbing: the materialize context, the materialized home, the protocol."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .._fs import deep_merge
from .._shell import shell_join
from ..constants import REDACTED
from ..placeholders import resolve
from ..prompt import Prompt
from ..spec import Spec

if TYPE_CHECKING:
    from ..parsers import Outcome

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
    run_id: str
    run_dir: Path
    home: Path
    argv: list[str]
    env: dict[str, str] = field(repr=False)
    stdin: str
    warnings: list[str]

    def redacted_env(self) -> dict[str, str]:
        return {k: (REDACTED if SECRET_ENV.search(k) else v) for k, v in self.env.items()}


class StreamParser(Protocol):
    def feed(self, obj: dict) -> list[tuple[str, dict]]: ...
    def running_cost(self) -> float | None: ...
    def outcome(self) -> Outcome: ...


class Adapter(Protocol):
    name: str

    def validate(self, spec: Spec) -> list[str]: ...
    def materialize(self, ctx: MaterializeContext) -> tuple[list[str], dict[str, str]]: ...
    def credential_env(self, spec: Spec, home: Path) -> dict[str, str]: ...
    def resume_argv(self, base_argv: list[str], session_id: str, spec: Spec) -> list[str]: ...
    def parser(self) -> StreamParser: ...
