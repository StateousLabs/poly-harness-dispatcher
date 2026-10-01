"""The pure step (CoreDesign §2c): write a run's isolated config home; run nothing."""

import shutil
from pathlib import Path

from .adapters import get_adapter
from .adapters.base import MaterializeContext, MaterializedHome
from .prompt import assemble
from .spec import Inputs, Spec


def materialize(spec: Spec, inputs: Inputs, *, run_id: str, run_dir: Path) -> MaterializedHome:
    adapter = get_adapter(spec.harness)
    home = run_dir / "home"
    home.mkdir(parents=True)
    stack_dir = home / "stack"  # read-only copy so ${stack} paths outlive the source stack
    shutil.copytree(spec.stack.root, stack_dir)
    prompt = assemble(spec.stack.identity, inputs, spec.cwd, spec.project_config)
    ctx = MaterializeContext(spec=spec, prompt=prompt, run_id=run_id, run_dir=run_dir, home=home, stack_dir=stack_dir)
    argv, env = adapter.materialize(ctx)
    return MaterializedHome(run_id=run_id, run_dir=run_dir, home=home, argv=argv, env=env,
                            stdin=prompt.user, warnings=ctx.warnings)
