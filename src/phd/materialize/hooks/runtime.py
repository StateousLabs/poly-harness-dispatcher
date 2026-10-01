"""Write the per-run hook runtime (shim + resolved hooks + shim config) into the run home."""

from importlib import resources
from pathlib import Path

from ..._util.fs import write_json


def write_runtime(home: Path, *, run_id: str, run_dir: Path, hooks: list[dict]) -> Path:
    phd_dir = home / "phd"
    phd_dir.mkdir(parents=True, exist_ok=True)
    shim = phd_dir / "hook_shim.py"
    shim.write_text(resources.files("phd.materialize.hooks").joinpath("shim.py").read_text(encoding="utf-8"), encoding="utf-8")
    write_json(phd_dir / "hooks.resolved.json", hooks)
    write_json(phd_dir / "shim_config.json", {"run_id": run_id, "run_dir": str(run_dir)})
    return shim
