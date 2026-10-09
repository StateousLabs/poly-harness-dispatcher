"""A real run of each harness leaves the host's harness folders untouched (item 8). Costs cents. Run with:

    PHD_LIVE=1 uv run pytest tests/live/test_host_isolation.py -v

What is snapshotted (path, size, mtime; recursive, bounded):
- `~/.codex`, `~/.local/share/opencode`, `~/.cache/opencode`, `~/.local/state/opencode`: in full. These are written
  only by codex/opencode processes, so any change during the run is a leak (from the run or the model catalogue).
- `~/.claude`: not file by file. The test itself usually runs inside a Claude Code session that keeps writing its
  own transcripts, history and telemetry there. Instead: no new folder in `~/.claude/projects` (Claude would create
  one for the run's cwd), and neither `~/.claude.json` nor `~/.claude/history.jsonl` mentions the run's cwd.
"""

import os
import shutil
from pathlib import Path

import pytest

from phd import Credentials, Dispatcher, Inputs, RunStatus, Spec, Stack

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("PHD_LIVE") != "1", reason="set PHD_LIVE=1 to run real harnesses")]

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
RUNS = ROOT / ".phd-test-runs"  # not under %TEMP%: codex refuses a temp CODEX_HOME
MODELS = {"claude": os.environ.get("PHD_LIVE_CLAUDE_MODEL", "haiku"),
          "codex": os.environ.get("PHD_LIVE_CODEX_MODEL", "gpt-5.6-terra"),
          "opencode": os.environ.get("PHD_LIVE_OPENCODE_MODEL", "opencode/space-bunny-free")}
HOME = Path.home()
SNAPSHOT_ROOTS = [HOME / ".codex", HOME / ".local" / "share" / "opencode", HOME / ".cache" / "opencode",
                  HOME / ".local" / "state" / "opencode"]
MAX_FILES = 200_000


def snapshot() -> dict[str, tuple[int, int]]:
    seen: dict[str, tuple[int, int]] = {}
    for root in SNAPSHOT_ROOTS:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                seen[path] = (st.st_size, st.st_mtime_ns)
                assert len(seen) < MAX_FILES, "snapshot too large; narrow SNAPSHOT_ROOTS"
    return seen


def claude_projects() -> set[str]:
    projects = HOME / ".claude" / "projects"
    return {p.name for p in projects.iterdir()} if projects.is_dir() else set()


def mentions(path: Path, needle: str) -> bool:
    if not path.is_file():
        return False
    variants = {needle, needle.replace("\\", "\\\\"), needle.replace("\\", "/")}
    text = path.read_text(encoding="utf-8", errors="replace")
    return any(v in text for v in variants)


def hookless_stack(tmp_path: Path) -> Stack:
    """The fixture stack without its hook: opencode's plugin dependency install can hang (a separate issue)."""
    root = tmp_path / "stack"
    shutil.copytree(FIXTURES / "stack", root)
    shutil.rmtree(root / "hooks")
    return Stack.load(root)


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode"])
def test_a_run_leaves_the_host_harness_folders_untouched(harness, tmp_path):
    cwd = tmp_path / f"isolation-{harness}"
    cwd.mkdir()
    stack = hookless_stack(tmp_path) if harness == "opencode" else Stack.load(FIXTURES / "stack")
    spec = Spec(harness=harness, model=MODELS[harness], stack=stack, cwd=cwd,
                credentials=Credentials.from_host(harness), secrets={"ECHO_PREFIX": "phd-echo"}, timeout_s=180)
    before, projects_before = snapshot(), claude_projects()

    r = Dispatcher(spec, runs_root=RUNS).spawn(Inputs(msg="Reply with the word hi."))  # includes the catalogue

    assert r.status is RunStatus.SUCCEEDED, (r.error, r.stderr_path)
    after = snapshot()
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    assert changed == [], f"host harness files changed: {changed}"
    assert claude_projects() - projects_before == set()
    for f in (HOME / ".claude.json", HOME / ".claude" / "history.jsonl"):
        assert not mentions(f, str(cwd)), f"{f} mentions the run's cwd"
