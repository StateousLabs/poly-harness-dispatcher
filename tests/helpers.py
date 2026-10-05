import json
import tarfile
import time
import zipfile
from pathlib import Path

from conftest import STREAMS
from phd._util.proc import pid_alive
from phd.harnesses import get_adapter
from phd.materialize import materialize


def wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def feed_all(parser, name):
    events = []
    for line in (STREAMS / name).read_text(encoding="utf-8").splitlines():
        events += parser.feed(json.loads(line))
    return events, parser.outcome()


def materialize_spec(spec, inputs, **kw):
    return materialize(get_adapter(spec.harness), spec, inputs, **kw)


def make_tar(src: Path, out: Path, *, top: str | None = None) -> Path:
    """tar.gz of src's contents; with `top`, everything sits under that one folder."""
    with tarfile.open(out, "w:gz") as tf:
        for p in sorted(src.rglob("*")):
            arc = p.relative_to(src).as_posix()
            tf.add(p, arcname=f"{top}/{arc}" if top else arc, recursive=False)
    return out


def make_zip(src: Path, out: Path, *, top: str | None = None) -> Path:
    with zipfile.ZipFile(out, "w") as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                arc = p.relative_to(src).as_posix()
                zf.write(p, f"{top}/{arc}" if top else arc)
    return out
