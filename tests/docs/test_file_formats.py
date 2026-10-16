"""The examples on docs/reference/file-formats.md are real: load them with the real validators.

Fenced blocks with a `title="<root>/<path>"` attribute are written out as files; the first path
component groups them into one directory per example.
"""

import re
from pathlib import Path

import pytest

from phd import Stack, stacks
from phd.spec.output_schema import schema_error

PAGE = Path(__file__).resolve().parents[2] / "docs" / "reference" / "file-formats.md"
BLOCK = re.compile(r"^```(\w+)(?: title=\"([^\"]+)\")?\n(.*?)^```", re.DOTALL | re.MULTILINE)


def _blocks():
    return [(lang, title, body) for lang, title, body in BLOCK.findall(PAGE.read_text("utf-8"))]


@pytest.fixture
def examples(tmp_path):
    for _, title, body in _blocks():
        if title:
            target = tmp_path / title
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
    return tmp_path


def test_the_page_has_titled_examples():
    assert len([t for _, t, _ in _blocks() if t]) >= 10


def test_minimal_stack_loads(examples):
    stack = Stack.load(examples / "minimal")
    assert (stack.name, stack.version, stack.agent_name) == ("minimal", "0.1.0", "helper")


def test_full_stack_loads(examples):
    stack = Stack.load(examples / "reviewer")
    assert stack.name == "reviewer"
    assert list(stack.harnesses) == ["claude", "codex", "opencode"]
    assert [getattr(s, "name", s) for s in stack.skills] == ["pytest-notes"]
    assert set(stack.mcp_servers) == {"echo", "docs"}
    assert len(stack.hooks) == 1


def test_manifest_composes_on_top_of_the_full_stack(examples, tmp_path):
    manifest = stacks.load_manifest(examples / "compose" / "stack.toml")
    assert manifest.name == "reviewer-plus"
    stack = stacks.resolve(examples / "compose" / "stack.toml", store_root=tmp_path / "store")
    assert stack.name == "reviewer-plus"
    assert {"pytest-notes", "lint-notes"} <= {getattr(s, "name", s) for s in stack.skills}


def test_output_schema_example_is_a_valid_schema():
    block = next(b for lang, t, b in _blocks() if lang == "python" and "verdict = {" in b)
    namespace: dict = {}
    exec(block.split("spec = ")[0].replace("from phd import Spec, Stack\n", ""), namespace)
    assert schema_error(namespace["verdict"]) is None
