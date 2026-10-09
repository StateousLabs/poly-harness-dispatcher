"""The pure step (CoreDesign §2c): write a run's isolated config home; run nothing."""

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from .._util.fs import remove_path
from ..spec import Inputs, Spec
from .context import MaterializeContext, MaterializedHome
from .hooks import check_supported, claude_style_block, opencode_plugin, write_runtime
from .prompt import Prompt, assemble

if TYPE_CHECKING:
    from ..harnesses import Adapter


def materialize(adapter: "Adapter", spec: Spec, inputs: Inputs, *, run_id: str, run_dir: Path,
                home: Path | None = None, rules_text: str | None = None,
                executable: list[str] | None = None) -> MaterializedHome:
    """Write the run home for `spec` + `inputs` and return the launch description.

    With `home=None` a fresh `<run_dir>/home` is created. With an existing `home` (a resumed session's), every
    PHD-owned path in it (`stack/`, `phd/` and `adapter.owned_paths(home)`) is deleted and written again from
    `spec`, so removed skills or hooks disappear; everything else (the harness's session data) is left alone.
    `rules_text` (a resumed session's recorded rules) goes into the system prompt when `inputs.rules` is unset.
    `executable` is the harness's `executables` override, for adapters whose env depends on the install.
    """
    if home is None:
        home = run_dir / "home"
        home.mkdir(parents=True)
    else:
        for path in [home / "stack", home / "phd", *adapter.owned_paths(home)]:
            remove_path(path)
    stack_dir = home / "stack"  # read-only copy so ${stack} paths outlive the source stack
    shutil.copytree(spec.stack.root, stack_dir)
    prompt = assemble(spec.stack.identity, inputs, spec.cwd, spec.project_config, rules_text=rules_text)
    ctx = MaterializeContext(spec=spec, prompt=prompt, run_id=run_id, run_dir=run_dir, home=home, stack_dir=stack_dir,
                             executable=executable)
    argv, env = adapter.materialize(ctx)
    return MaterializedHome(run_id=run_id, run_dir=run_dir, home=home, argv=argv, env=env,
                            stdin=prompt.user, warnings=ctx.warnings, rules_text=prompt.rules)


__all__ = ["MaterializeContext", "MaterializedHome", "Prompt", "assemble", "check_supported", "claude_style_block",
           "materialize", "opencode_plugin", "write_runtime"]
