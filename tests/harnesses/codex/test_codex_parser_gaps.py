from phd.harnesses.codex.parser import CodexParser


def test_unexpected_shapes_become_an_unparsed_log_event():
    p = CodexParser()
    # `usage` is a string: .get on it raises inside the parser, which must degrade to a log event, not crash
    events = p.feed({"type": "turn.completed", "usage": "oops"})
    assert events == [("log", {"text": "unparsed codex event", "raw_type": "turn.completed"})]
    assert p.outcome().num_turns is None


def test_non_benign_error_item_is_logged_as_a_warning():
    p = CodexParser()
    events = p.feed({"type": "item.completed", "item": {"type": "error", "message": "rate limited, retrying"}})
    assert events == [("log", {"text": "rate limited, retrying", "level": "warning"})]
    assert p.outcome().error is None


def test_hook_trust_warning_is_swallowed():
    p = CodexParser()
    assert p.feed({"type": "item.completed",
                   "item": {"type": "error", "message": "ignored --dangerously-bypass-hook-trust"}}) == []


def test_error_item_that_is_only_started_and_unknown_items_emit_nothing():
    p = CodexParser()
    assert p.feed({"type": "item.started", "item": {"type": "error", "message": "early"}}) == []
    assert p.feed({"type": "item.completed", "item": {"type": "reasoning", "text": "thinking"}}) == []
    assert p.outcome().result_text is None
