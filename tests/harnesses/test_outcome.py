import json

import pytest

from conftest import STREAMS
from phd.harnesses.codex.parser import CodexParser
from phd.harnesses.opencode.parser import OpencodeParser
from phd.harnesses.outcome import classify_error


def test_running_cost():
    p = OpencodeParser()
    p.feed(json.loads((STREAMS / "opencode_costly.jsonl").read_text(encoding="utf-8")))
    assert p.running_cost() == pytest.approx(0.5)
    assert CodexParser().running_cost() is None


@pytest.mark.parametrize("message, kind", [
    ("The model `x` does not exist or you do not have access to it.", "model_unavailable"),
    ("Insufficient account funds", "billing"),
    ("401 Unauthorized", "auth"),
    ("Exceeded max budget", "budget"),
    ("something else broke", "harness_error"),
])
def test_classify_error(message, kind):
    assert classify_error(message) == kind


@pytest.mark.parametrize("message, kind", [
    ("request 14012 failed", "harness_error"),
    ("HTTP 402 Payment Required", "billing"),
    ("status 401 unauthorized", "auth"),
])
def test_classify_error_with_boundaries(message, kind):
    """classify_error should use word boundaries for error codes."""
    assert classify_error(message) == kind
