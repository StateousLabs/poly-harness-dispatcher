import json
import time

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
