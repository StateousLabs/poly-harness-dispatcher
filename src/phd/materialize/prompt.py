"""Prompt assembly (CoreDesign §4): identity + rules (+ project context) go to the system
prompt; brief + msg form the first user message, delivered on stdin."""

from dataclasses import dataclass
from pathlib import Path

from ..spec import Inputs

PROJECT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str
    rules: str | None = None  # the rules block's content (stripped), recorded so a resumed session re-applies it


def assemble(identity: str, inputs: Inputs, cwd: Path, project_config: str, *,
             rules_text: str | None = None) -> Prompt:
    """Build the system prompt and first user message.

    `rules_text` is a resumed session's recorded rules; it is used when `inputs.rules` is not given.
    """
    system = [identity.strip()]
    rules = inputs.rules.read_text(encoding="utf-8").strip() if inputs.rules is not None else rules_text
    if rules is not None:
        rules = rules.strip()
        system.append("## Project rules (binding)\n\n" + rules)
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
    return Prompt(system="\n\n".join(system) + "\n", user="\n\n".join(user) + "\n", rules=rules)
