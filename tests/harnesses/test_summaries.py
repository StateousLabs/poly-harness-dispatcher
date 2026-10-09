"""Event.summary: per-harness raw summaries and the shared one-line formatting."""

import json

from conftest import STREAMS
from phd.harnesses.claude.parser import ClaudeParser
from phd.harnesses.codex.parser import CodexParser
from phd.harnesses.opencode.parser import OpencodeParser
from phd.harnesses.summary import MAX_SUMMARY, finish
from phd.results import Event


def summaries(parser, name):
    out = []
    for line in (STREAMS / name).read_text(encoding="utf-8").splitlines():
        for kind, data in parser.feed(json.loads(line)):
            out.append((kind, finish(kind, parser.summarize(kind, data))))
    return out


def only_summarised(pairs):
    return [(k, s) for k, s in pairs if k in ("message", "tool_call", "tool_result")]


def test_event_has_summary_field_in_to_dict():
    ev = Event(run_id="r", harness="claude", type="log", data={}, ts=1.0)
    assert ev.summary == "" and ev.to_dict()["summary"] == ""
    assert Event(run_id="r", harness="claude", type="message", data={}, ts=1.0, summary="hi").to_dict()["summary"] == "hi"


def test_claude_fixture():
    assert only_summarised(summaries(ClaudeParser(), "claude_ok.jsonl")) == [
        ("message", "Checking."), ("tool_call", "Bash: echo hello-from-shell"), ("tool_result", "Bash: ok")]


def test_claude_failed_result_and_key_arguments():
    p = ClaudeParser()
    calls = [("Read", {"file_path": "a.py"}), ("NotebookEdit", {"notebook_path": "n.ipynb"}), ("Grep", {"pattern": "foo"}),
             ("WebFetch", {"url": "http://x"}), ("WebSearch", {"query": "q"}), ("Task", {"description": "dig"}),
             ("mcp__srv__tool", {"x": 1}), ("WebSearch", {}), ("Edit", {"file_path": "src/app.py"})]
    want = ["Read: a.py", "NotebookEdit: n.ipynb", "Grep: foo", "WebFetch: http://x", "WebSearch: q", "Task: dig",
            "mcp__srv__tool", "WebSearch", "Edit: src/app.py"]
    got = []
    for i, (name, inp) in enumerate(calls):
        for kind, data in p.feed({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}}):
            got.append(p.summarize(kind, data))
    assert got == want
    [(kind, data)] = p.feed({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t0", "is_error": True}]}})
    assert p.summarize(kind, data) == "Read: failed"


def test_codex_fixture():
    assert only_summarised(summaries(CodexParser(), "codex_ok.jsonl")) == [
        ("message", "I'll run the checks."),
        ("tool_call", "Shell: echo hello-from-shell"), ("tool_result", "Shell: ok"),
        ("tool_call", "mcp:echo/echo"), ("tool_result", "mcp:echo/echo: ok"),
        ("message", "HELPER-IDENTITY-OK")]


def test_codex_file_change_web_search_and_failure():
    p = CodexParser()

    def feed(kind, item):
        return [p.summarize(k, d) for k, d in p.feed({"type": kind, "item": item})]

    assert feed("item.started", {"id": "1", "type": "file_change", "changes": [{"path": "a.py", "kind": "update"}],
                                 "status": "in_progress"}) == ["Edit: a.py"]
    assert feed("item.started", {"id": "2", "type": "file_change", "status": "in_progress"}) == ["Edit"]
    assert feed("item.started", {"id": "3", "type": "web_search", "query": "pytest", "status": "in_progress"}) == [
        "WebSearch: pytest"]
    assert feed("item.completed", {"id": "4", "type": "command_execution", "command": "x", "exit_code": 1,
                                   "status": "completed"}) == ["Shell: failed"]
    assert feed("item.completed", {"id": "5", "type": "command_execution", "command": "x", "status": "failed"}) == [
        "Shell: failed"]


def test_opencode_fixture_call_then_result():
    pairs = summaries(OpencodeParser(), "opencode_ok.jsonl")
    assert [k for k, _ in pairs] == ["tool_call", "tool_result", "usage", "message", "usage"]
    assert only_summarised(pairs) == [("tool_call", "Bash: echo hello-from-shell"), ("tool_result", "Bash: ok"),
                                      ("message", "HELPER-IDENTITY-OK")]


def test_opencode_error_status_and_keys():
    p = OpencodeParser()

    def one(tool, inp, status="completed"):
        evs = p.feed({"type": "tool_use", "part": {"tool": tool, "callID": "c", "state": {"status": status, "input": inp}}})
        assert [k for k, _ in evs] == ["tool_call", "tool_result"]
        return [p.summarize(k, d) for k, d in evs]

    assert one("bash", {"command": "false"}, "error") == ["Bash: false", "Bash: failed"]
    assert one("read", {"filePath": "/a/b.py"}) == ["Read: /a/b.py", "Read: ok"]
    assert one("grep", {"pattern": "p"}) == ["Grep: p", "Grep: ok"]
    assert one("webfetch", {"url": "u"}) == ["Webfetch: u", "Webfetch: ok"]
    assert one("todowrite", {"todos": []}) == ["Todowrite", "Todowrite: ok"]


def test_finish_rules():
    assert finish("tool_call", "Bash: a\n  b\t\r\nc") == "Bash: a b c"
    assert finish("message", "\n\n  first   line \nsecond\n\nthird") == "first line"
    assert finish("message", "") == ""
    assert finish("usage", "anything") == ""
    long = finish("message", "x" * 600)
    assert len(long) == MAX_SUMMARY == 500 and long.endswith("…") and long[:-1] == "x" * 499
    assert finish("tool_call", "y" * 500) == "y" * 500
