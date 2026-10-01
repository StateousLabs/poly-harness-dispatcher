"""Prompt assembly (CoreDesign §4): identity + rules (+ project context) go to the system
prompt; brief + msg form the first user message, delivered on stdin."""

from dataclasses import dataclass
from pathlib import Path

from .errors import EmptyTask
from .spec import Inputs

PROJECT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str


def assemble(identity: str, inputs: Inputs, cwd: Path, project_config: str) -> Prompt:
    system = [identity.strip()]
    if inputs.rules is not None:
        system.append("## Project rules (binding)\n\n" + inputs.rules.read_text(encoding="utf-8").strip())
    if project_config == "instructions_only":
        for name in PROJECT_INSTRUCTION_FILES:
            path = cwd / name
            if path.is_file():
                system.append(f"## Project context ({name})\n\n" + path.read_text(encoding="utf-8").strip())

    user = []
    if inputs.brief is not None:
        user.append("# Task brief\n\n" + inputs.brief.read_text(encoding="utf-8").strip())
    if inputs.msg and inputs.msg.strip():
        user.append("# Message\n\n" + inputs.msg.strip())
    return Prompt(system="\n\n".join(system) + "\n", user="\n\n".join(user) + "\n")


def resume_message(msg: str) -> str:
    if not msg or not msg.strip():
        raise EmptyTask("resume needs a non-empty msg")
    return "# Message\n\n" + msg.strip() + "\n"
