import json

import pytest

from phd.errors import ModelNotSupported
from phd.models import ModelCatalog

CODEX = json.dumps({"models": [{"slug": "gpt-5.6-terra"}, {"slug": "gpt-5.6-luna"}, {"slug": "gpt-5.5"}]})
OPENCODE = "opencode/big-pickle\nopencode/deepseek-v4-flash\nsome log line\n"


def runner(argv):
    if argv[:3] == ["codex", "debug", "models"]:
        return CODEX
    if argv[:2] == ["opencode", "models"]:
        return OPENCODE
    raise AssertionError(argv)


def test_codex_catalogue():
    cat = ModelCatalog(runner)
    assert cat.models("codex") == ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"]
    assert cat.validate("codex", "gpt-5.6-terra") == []
    with pytest.raises(ModelNotSupported) as e:
        cat.validate("codex", "gpt-5.6-tera")
    assert e.value.hint == "gpt-5.6-terra" and "gpt-5.6-luna" in str(e.value)


def test_opencode_catalogue_ignores_non_model_lines():
    cat = ModelCatalog(runner)
    assert cat.models("opencode") == ["opencode/big-pickle", "opencode/deepseek-v4-flash"]
    with pytest.raises(ModelNotSupported):
        cat.validate("opencode", "big-pickle")


def test_claude_rules():
    cat = ModelCatalog(runner)
    assert cat.validate("claude", "haiku") == []
    assert cat.validate("claude", "opus[1m]") == []
    assert cat.validate("claude", "claude-sonnet-5-5") == []
    assert "not in PHD's known list" in cat.validate("claude", "claude-haiku-5")[0]
    with pytest.raises(ModelNotSupported):
        cat.validate("claude", "gpt-4o")


def test_unavailable_catalogue_is_a_warning():
    def broken(argv):
        raise RuntimeError("codex exited 1")
    assert "was not checked" in ModelCatalog(broken).validate("codex", "anything")[0]


def test_catalogue_is_cached():
    calls = []
    def counting(argv):
        calls.append(argv)
        return CODEX
    cat = ModelCatalog(counting)
    cat.validate("codex", "gpt-5.5")
    cat.validate("codex", "gpt-5.6-luna")
    assert len(calls) == 1
