from pathlib import Path

import pytest

from phd.stack import Stack

FIXTURES = Path(__file__).parent / "fixtures"
STREAMS = FIXTURES / "streams"
FAKE_HARNESS = Path(__file__).parent / "fakes" / "fake_harness.py"


@pytest.fixture
def stack() -> Stack:
    return Stack.load(FIXTURES / "stack")
