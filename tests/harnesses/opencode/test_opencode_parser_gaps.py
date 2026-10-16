from phd.harnesses.opencode.parser import OpencodeParser


def test_unexpected_shapes_become_an_unparsed_log_event():
    p = OpencodeParser()
    # `part` is a string: .get on it raises inside the parser, which must degrade to a log event, not crash
    events = p.feed({"type": "text", "part": "oops"})
    assert events == [("log", {"text": "unparsed opencode event", "raw_type": "text"})]
    assert p.outcome().result_text is None
