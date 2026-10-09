"""Per-harness output_schema translation: wrapper for non-object roots, Codex strict mode, answer restore."""

import copy

import pytest

from phd.errors import UnsupportedFeature
from phd.harnesses import get_adapter
from phd.harnesses.codex.schema import restore, to_strict
from phd.harnesses.schema_wrap import needs_wrap, unwrap, wrap

DONE = {"type": "object", "properties": {"event": {"const": "done"},
                                         "payload": {"type": "object", "properties": {"summary": {"type": "string"}},
                                                     "required": ["summary"]}},
        "required": ["event"], "additionalProperties": False}
BLOCKED = {"type": "object", "properties": {"event": {"const": "blocked"}, "reason": {"type": "string"}},
           "required": ["event", "reason"], "additionalProperties": False}
TWO_BRANCH = {"oneOf": [DONE, BLOCKED]}


def test_needs_wrap_only_for_non_object_roots():
    assert needs_wrap(TWO_BRANCH)
    assert needs_wrap({"anyOf": [DONE]}) and needs_wrap({"$ref": "#/$defs/x"}) and needs_wrap({"type": "array"})
    assert needs_wrap({"type": "object", "oneOf": [DONE]})
    assert not needs_wrap({"type": "object", "properties": {}})


def test_wrap_and_unwrap():
    w = wrap({"$schema": "https://json-schema.org/draft/2020-12/schema", "$defs": {"a": {"type": "string"}}, **TWO_BRANCH})
    assert w == {"$schema": "https://json-schema.org/draft/2020-12/schema", "$defs": {"a": {"type": "string"}},
                 "type": "object", "properties": {"outcome": TWO_BRANCH}, "required": ["outcome"],
                 "additionalProperties": False}
    assert unwrap({"outcome": {"event": "done"}}) == {"event": "done"}
    assert unwrap("text") == "text"


def test_codex_strict_conversion_matches_the_accepted_live_shape():
    done = {"type": "object", "properties": {"event": {"const": "done"},
            "payload": {"anyOf": [{"type": "object", "properties": {"summary": {"type": "string"}},
                                   "required": ["summary"], "additionalProperties": False}, {"type": "null"}]}},
            "required": ["event", "payload"], "additionalProperties": False}
    assert to_strict(TWO_BRANCH) == {
        "type": "object", "properties": {"outcome": {"anyOf": [done, BLOCKED]}}, "required": ["outcome"],
        "additionalProperties": False}


def test_conversion_does_not_mutate_the_callers_schema():
    before = copy.deepcopy(TWO_BRANCH)
    to_strict(TWO_BRANCH)
    assert TWO_BRANCH == before


def test_codex_object_root_is_converted_but_not_wrapped():
    s = to_strict({"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}, "required": ["a"]})
    assert s == {"type": "object",
                 "properties": {"a": {"type": "string"}, "b": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
                 "required": ["a", "b"], "additionalProperties": False}


def test_codex_converts_defs_items_and_nested_oneof():
    s = to_strict({"type": "object", "properties": {"xs": {"type": "array", "items": {"$ref": "#/$defs/i"}}},
                   "required": ["xs"],
                   "$defs": {"i": {"oneOf": [{"type": "object", "properties": {"k": {"type": "string"}}}]}}})
    assert s["$defs"]["i"] == {"anyOf": [{"type": "object", "properties": {
        "k": {"anyOf": [{"type": "string"}, {"type": "null"}]}}, "required": ["k"], "additionalProperties": False}]}


def test_restore_drops_optional_nulls_but_keeps_explicitly_nullable():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "n": {"type": ["string", "null"]},
                                               "m": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
              "required": ["n", "m"]}
    assert restore(schema, {"a": None, "n": None, "m": None}) == {"n": None, "m": None}


def test_restore_unwraps_and_strips_by_branch():
    assert restore(TWO_BRANCH, {"outcome": {"event": "done", "payload": None}}) == {"event": "done"}
    assert restore(TWO_BRANCH, {"outcome": {"event": "done", "payload": {"summary": "s"}}}) == {
        "event": "done", "payload": {"summary": "s"}}
    nested = {"type": "object", "properties": {"xs": {"type": "array", "items": {
        "type": "object", "properties": {"k": {"type": "string"}}}}}, "required": ["xs"]}
    assert restore(nested, {"xs": [{"k": None}, {"k": "v"}]}) == {"xs": [{}, {"k": "v"}]}


@pytest.mark.parametrize("keyword,schema", [
    ("patternProperties", {"type": "object", "patternProperties": {"^a": {"type": "string"}}}),
    ("not", {"type": "object", "properties": {"a": {"not": {"type": "null"}}}}),
    ("if", {"type": "object", "if": {}, "then": {}}),
    ("dependentSchemas", {"type": "object", "dependentSchemas": {"a": {}}})])
def test_unsupported_keywords_refused_for_codex_only(make_spec, keyword, schema):
    with pytest.raises(UnsupportedFeature, match=keyword):
        get_adapter("codex").validate(make_spec("codex", output_schema=schema))
    get_adapter("claude").validate(make_spec("claude", output_schema=schema))
    get_adapter("opencode").validate(make_spec("opencode", output_schema=schema))


FREE_FORM = "free-form object"


@pytest.mark.parametrize("schema,match", [
    ({"type": "object"}, FREE_FORM),
    ({"type": "object", "properties": {"a": {"type": "object"}}}, FREE_FORM),
    ({"type": "object", "properties": {"a": {"type": ["object", "null"]}}}, FREE_FORM),
    ({"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True}, "additionalProperties"),
    ({"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": {"type": "string"}},
     "additionalProperties"),
    ({"type": "object", "properties": {"a": {"type": "string"}}, "allOf": [{"required": ["a"]}]}, "allOf"),
    ({"type": "object", "properties": {"a": {"type": "string"}}, "oneOf": [{}], "anyOf": [{}]}, "oneOf.*anyOf"),
    ({"type": "object", "properties": {"a": {"type": "string"}}, "dependencies": {"a": ["a"]}}, "dependencies"),
    ({"type": "array", "items": [{"type": "string"}]}, "items"),
    ({"type": "array", "items": {"type": "string"}, "additionalItems": False}, "additionalItems"),
    ({"type": "array", "items": {"type": "string"}, "contains": {"type": "string"}}, "contains"),
    ({"type": "object", "properties": {"a": {"type": "string"}}, "propertyNames": {"pattern": "^a"}}, "propertyNames")])
def test_codex_refuses_what_strict_mode_cannot_express(schema, match):
    with pytest.raises(UnsupportedFeature, match=match):
        to_strict(schema)


def test_free_form_message_names_the_path():
    with pytest.raises(UnsupportedFeature, match=r"at \$/properties/a\); give it properties"):
        to_strict({"type": "object", "properties": {"a": {"type": "object"}}})


def test_schema_key_is_dropped_for_codex_only():
    s = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "properties": {"a": {"type": "string"}}}
    assert "$schema" not in to_strict(s) and "$schema" not in to_strict(TWO_BRANCH | s)
    assert wrap(s)["$schema"] == s["$schema"]


def test_a_property_named_like_a_keyword_is_fine():
    to_strict({"type": "object", "properties": {"not": {"type": "string"}, "if": {"type": "string"}}})
