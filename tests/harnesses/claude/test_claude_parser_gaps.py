from phd.harnesses.claude.parser import ClaudeParser


def test_assistant_blocks_other_than_text_and_tool_use_are_ignored():
    p = ClaudeParser()
    events = p.feed({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "hmm"}, "stray string",
        {"type": "text", "text": "hi"}]}})
    assert events == [("message", {"text": "hi"})]


def test_user_blocks_other_than_tool_results_are_ignored():
    p = ClaudeParser()
    events = p.feed({"type": "user", "message": {"content": [
        {"type": "text", "text": "an injected note"}, "stray string",
        {"type": "tool_result", "tool_use_id": "t1", "is_error": True}]}})
    assert events == [("tool_result", {"id": "t1", "is_error": True})]


def test_result_carries_structured_output_and_cost():
    p = ClaudeParser()
    events = p.feed({"type": "result", "subtype": "success", "result": "done", "total_cost_usd": 0.25,
                     "structured_output": {"answer": 1}, "usage": {"input_tokens": 3, "output_tokens": 4}})
    assert [t for t, _ in events] == ["usage"]
    o = p.outcome()
    assert o.structured_output == {"answer": 1} and o.cost_usd == 0.25 and o.cost_source == "harness"
    assert p.running_cost() == 0.25 and o.error is None


def test_error_result_without_cost_is_unknown_cost():
    p = ClaudeParser()
    p.feed({"type": "result", "subtype": "error_max_budget_usd", "is_error": True})
    o = p.outcome()
    assert o.error.kind == "budget" and o.error.message == "error_max_budget_usd"
    assert o.cost_source == "unknown" and p.running_cost() is None
