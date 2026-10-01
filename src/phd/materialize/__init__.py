"""The pure step (CoreDesign §2c): write a run's isolated config home; run nothing."""

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ..spec import Inputs, Spec
from .context import MaterializeContext, MaterializedHome
from .hooks import check_supported, claude_style_block, opencode_plugin, write_runtime
from .prompt import Prompt, assemble, resume_message

if TYPE_CHECKING:
    from ..harnesses import Adapter


def materialize(adapter: "Adapter", spec: Spec, inputs: Inputs, *, run_id: str, run_dir: Path) -> MaterializedHome:
    home = run_dir / "home"
    home.mkdir(parents=True)
    stack_dir = home / "stack"  # read-only copy so ${stack} paths outlive the source stack
    shutil.copytree(spec.stack.root, stack_dir)
    prompt = assemble(spec.stack.identity, inputs, spec.cwd, spec.project_config)
    ctx = MaterializeContext(spec=spec, prompt=prompt, run_id=run_id, run_dir=run_dir, home=home, stack_dir=stack_dir)
    argv, env = adapter.materialize(ctx)
    return MaterializedHome(run_id=run_id, run_dir=run_dir, home=home, argv=argv, env=env,
                            stdin=prompt.user, warnings=ctx.warnings)


__all__ = ["MaterializeContext", "MaterializedHome", "Prompt", "assemble", "check_supported", "claude_style_block",
           "materialize", "opencode_plugin", "resume_message", "write_runtime"]
