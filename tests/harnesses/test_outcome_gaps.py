import pytest

from phd.harnesses.outcome import as_int, classify_error


@pytest.mark.parametrize("value, expected", [
    (7, 7), (7.9, 7), (-2.5, -2), ("42", 42), ("not a number", 0), ("", 0), ("4.5", 0), (None, 0), ([1], 0), ({}, 0),
])
def test_as_int_coerces_token_counts_defensively(value, expected):
    assert as_int(value) == expected


@pytest.mark.parametrize("message", [
    "No conversation found with session ID: abc",
    "Error: thread/resume: thread/resume failed: no rollout found for thread id abc (code -32600)",
    "\x1b[91mError: Session not found\x1b[0m",
])
def test_a_resume_of_a_vanished_session_is_classified_as_session(message):
    assert classify_error(message) == "session"


def test_a_session_message_from_an_mcp_server_is_not_the_harness_session():
    assert classify_error("MCP error: session not found") == "harness_error"
