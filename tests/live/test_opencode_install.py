"""OpenCode starts without its per-config-folder npm install, and hooks still fire (item 9). Free model. Run with:

    PHD_LIVE=1 uv run pytest tests/live/test_opencode_install.py -v

Before the fix a fresh home with hooks (a plugin) stalled 17-100+ s while opencode npm-installed
`@opencode-ai/plugin` into both config folders, and wrote to the host's npm cache. A home whose install was killed
also made the next turn wait 60 s for opencode's stale-lock rule."""

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from phd import Credentials, Dispatcher, Inputs, RunStatus, Spec, Stack

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("PHD_LIVE") != "1", reason="set PHD_LIVE=1 to run real harnesses")]

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
RUNS = ROOT / ".phd-test-runs"
MODEL = os.environ.get("PHD_LIVE_OPENCODE_MODEL", "opencode/space-bunny-free")
FIRST_EVENT_S = 12  # first harness event (not PHD's run.started): fixed about 2-6 s; before: 17 s at the least
RUN_S = 60


def host_npm_index() -> Path:
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "npm-cache" / "_cacache" / "index-v5"
    return Path.home() / ".npm" / "_cacache" / "index-v5"


def npm_snapshot() -> dict[str, tuple[int, int]]:
    """Every entry of the host npm cache index (read only): about 1 s for a few thousand entries."""
    root, out = host_npm_index(), {}
    if root.is_dir():
        for p in root.rglob("*"):
            if p.is_file():
                st = p.stat()
                out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


def dispatcher(cwd: Path, stack: Stack | None = None) -> Dispatcher:
    spec = Spec(harness="opencode", model=MODEL, stack=stack or Stack.load(FIXTURES / "stack"), cwd=cwd,
                credentials=Credentials.from_host("opencode"), secrets={"ECHO_PREFIX": "phd-echo"}, timeout_s=300)
    return Dispatcher(spec, runs_root=RUNS)


async def timed_run(d: Dispatcher, inputs: Inputs, session_id: str | None = None):
    t0 = time.monotonic()
    h = await d.start(inputs, session_id=session_id)
    first = None
    async for e in h.events():
        if first is None and e.type not in ("run.started", "log"):  # opencode's own output, after its startup
            first = time.monotonic() - t0
    r = await h.wait()
    return r, first, time.monotonic() - t0


async def test_fresh_home_with_hooks_starts_fast_and_leaves_host_npm_cache_alone(tmp_path):
    before = npm_snapshot()
    r, first, total = await timed_run(dispatcher(tmp_path), Inputs(msg="Reply with the word hi."))
    assert r.status is RunStatus.SUCCEEDED, (r.error, r.stderr_path)
    assert first is not None and first < FIRST_EVENT_S, f"first event after {first:.1f}s (plugin install stall?)"
    assert total < RUN_S, f"run took {total:.1f}s"
    after = npm_snapshot()
    changed = sorted(k for k in after if before.get(k) != after[k])
    assert changed == [], f"host npm cache gained {len(changed)} entries, e.g. {changed[:3]}"


async def test_resume_on_a_killed_install_home_does_not_wait_for_its_lock(tmp_path):
    """Reproduce what a pre-fix run killed mid-install left behind (package.json and a partial node_modules, no
    package-lock.json, a lock with a fresh heartbeat), then resume: no install starts, so the lock is never waited on."""
    d = dispatcher(tmp_path)
    first, _, _ = await timed_run(d, Inputs(msg="Remember the code word ZEBRA-42. Reply briefly."))
    assert first.status is RunStatus.SUCCEEDED, first.error
    home = Path(first.home_dir)
    locks = home / "state" / "opencode" / "locks"
    for folder in (home / "config", home / "xdg-config" / "opencode"):
        (folder / "package-lock.json").unlink()
        (folder / "node_modules" / "partial").mkdir(parents=True, exist_ok=True)
        lock = locks / (hashlib.sha1(f"npm-install:{folder}".encode()).hexdigest() + ".lock")
        lock.mkdir(parents=True, exist_ok=True)
        (lock / "heartbeat").write_text("", encoding="utf-8")
        (lock / "meta.json").write_text(json.dumps({"token": "t", "pid": 1, "hostname": "h",
                                                    "createdAt": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    r, first_event, total = await timed_run(d, Inputs(msg="What was the code word?"), session_id=first.session_id)
    assert r.status is RunStatus.SUCCEEDED, (r.error, r.stderr_path)
    assert "ZEBRA-42" in (r.result_text or ""), r.result_text
    assert first_event is not None and first_event < FIRST_EVENT_S, f"first event after {first_event:.1f}s"
    assert total < RUN_S, f"resumed turn took {total:.1f}s (stale-lock wait?)"


MARK_HOOK = """import json, os, sys
evt = json.load(sys.stdin)
with open(os.path.join(os.environ["PHD_RUN_DIR"], "read-hook.log"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"event": evt.get("event"), "tool": evt.get("tool")}) + "\\n")
"""


def test_post_tool_hook_fires_on_read_under_default_permissions(tmp_path):
    """A neutral `post_tool` hook on `read` (allowed by PHD's restricted OpenCode permissions) fires through the
    generated plugin."""
    stack_dir = tmp_path / "stack"
    shutil.copytree(FIXTURES / "stack", stack_dir)
    (stack_dir / "hooks" / "mark.py").write_text(MARK_HOOK, encoding="utf-8")
    (stack_dir / "hooks" / "hooks.json").write_text(json.dumps({"hooks": [
        {"event": "post_tool", "match": {"tool": "read"}, "command": "python ${stack}/hooks/mark.py",
         "timeout_s": 10}]}), encoding="utf-8")
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "notes.txt").write_text("The secret fruit is KIWI-7.\n", encoding="utf-8")
    r = dispatcher(cwd, Stack.load(stack_dir)).spawn(
        Inputs(msg="Use your read tool to read the file notes.txt in the current directory, then reply with the "
                   "secret fruit it names."))
    assert r.status is RunStatus.SUCCEEDED, (r.error, r.stderr_path)
    assert "KIWI-7" in (r.result_text or ""), r.result_text
    log = Path(r.run_dir) / "read-hook.log"
    assert log.is_file(), "post_tool hook on read did not fire"
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert {"event": "post_tool", "tool": "read"} in entries, entries
