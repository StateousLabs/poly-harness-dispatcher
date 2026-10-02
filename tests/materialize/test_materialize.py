import json
import shutil

import pytest

from conftest import FIXTURES
from helpers import materialize_spec
from phd.spec import Inputs, Stack

# harness-owned session data PHD must never touch when it re-materializes into an existing home
SESSION_FILES = {"claude": ["projects/p/session.jsonl", ".claude.json"],
                 "codex": ["sessions/2026/rollout.jsonl", "state.sqlite"],
                 "opencode": ["data/opencode/storage/session.json", "config/package.json"]}
SKILLS_DIR = {"claude": "skills", "codex": "skills", "opencode": "config/skills"}
HOOK_FILES = {"claude": "phd", "codex": "hooks.json", "opencode": "config/plugin/phd-hooks.js"}


def slim_stack(tmp_path) -> Stack:
    """The fixture stack without the demo-minimal skill and without hooks."""
    root = tmp_path / "slim-stack"
    shutil.copytree(FIXTURES / "stack", root)
    shutil.rmtree(root / "skills" / "demo-minimal")
    shutil.rmtree(root / "hooks")
    return Stack.load(root)


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode"])
def test_rematerialize_into_existing_home(harness, tmp_path, make_spec, inputs):
    first = materialize_spec(make_spec(harness), inputs, run_id="r1", run_dir=tmp_path / "r1")
    home = first.home
    assert (home / SKILLS_DIR[harness] / "demo-minimal" / "SKILL.md").is_file()
    assert (home / HOOK_FILES[harness]).exists()
    for rel in SESSION_FILES[harness]:
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text("session", encoding="utf-8")

    second = materialize_spec(make_spec(harness, stack=slim_stack(tmp_path), autonomy="full", unsafe=True),
                              Inputs(msg="next"), run_id="r2", run_dir=tmp_path / "r2", home=home)

    assert second.home == home and not (tmp_path / "r2" / "home").exists()
    for rel in SESSION_FILES[harness]:
        assert (home / rel).read_text(encoding="utf-8") == "session", rel
    assert not (home / SKILLS_DIR[harness] / "demo-minimal").exists()
    assert (home / SKILLS_DIR[harness] / "demo-notes" / "SKILL.md").is_file()
    assert not (home / HOOK_FILES[harness]).exists()  # stale hooks removed with the stack that defined them
    assert not (home / "stack" / "skills" / "demo-minimal").exists()
    assert second.stdin == "# Message\n\nnext\n"


def test_rematerialize_points_hooks_at_new_run(tmp_path, make_spec, inputs):
    first = materialize_spec(make_spec("claude"), inputs, run_id="r1", run_dir=tmp_path / "r1")
    materialize_spec(make_spec("claude", harness_config={"env": {"X": "1"}}), inputs, run_id="r2",
                     run_dir=tmp_path / "r2", home=first.home)
    shim_config = json.loads((first.home / "phd" / "shim_config.json").read_text(encoding="utf-8"))
    assert shim_config == {"run_id": "r2", "run_dir": str(tmp_path / "r2")}
    settings = json.loads((first.home / "settings.json").read_text(encoding="utf-8"))
    assert settings["env"] == {"X": "1"}
