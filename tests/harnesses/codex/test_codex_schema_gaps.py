"""Edge cases of the codex strict-mode conversion and its inverse: boolean sub-schemas, local `$ref`s, nullable
branches, discriminated unions and runaway recursion."""

import pytest

from phd.errors import UnsupportedFeature
from phd.harnesses.codex.schema import restore, to_strict


def test_boolean_subschema_is_passed_through_to_strict():
    schema = {"type": "object", "properties": {"anything": True}, "required": ["anything"]}
    assert to_strict(schema)["properties"]["anything"] is True


def test_restore_follows_local_refs_to_drop_optional_nulls():
    schema = {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}, "required": ["a"],
              "$defs": {"A": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "string"}},
                              "required": ["x"]}}}
    assert restore(schema, {"a": {"x": 1, "y": None}}) == {"a": {"x": 1}}
    assert restore(schema, {"a": {"x": 1, "y": "s"}}) == {"a": {"x": 1, "y": "s"}}


def test_restore_decodes_json_pointer_escapes_in_refs():
    schema = {"type": "object", "properties": {"a": {"$ref": "#/$defs/a~1b"}, "b": {"$ref": "#/$defs/t~0"}},
              "required": ["a", "b"],
              "$defs": {"a/b": {"type": "object", "properties": {"x": {"type": "string"}}},
                        "t~": {"type": "object", "properties": {"y": {"type": "string"}}}}}
    assert restore(schema, {"a": {"x": None}, "b": {"y": None}}) == {"a": {}, "b": {}}


def test_restore_follows_a_root_ref_for_recursive_schemas():
    schema = {"type": "object", "properties": {"name": {"type": "string"}, "child": {"$ref": "#"}},
              "required": ["name"]}
    value = {"name": "a", "child": {"name": "b", "child": None}}
    # `child` is optional and not nullable, so the null Codex filled in is dropped at every level
    assert restore(schema, value) == {"name": "a", "child": {"name": "b"}}


@pytest.mark.parametrize("ref", ["#/$defs/missing", "#/properties/a/type/x", "#/properties/a/type"])
def test_restore_leaves_an_unresolvable_ref_alone(ref):
    schema = {"type": "object", "properties": {"a": {"$ref": ref}}, "required": ["a"]}
    # the unresolved node is the best knowledge there is; it has no null branch, but `a` is required, so it stays
    assert restore(schema, {"a": None}) == {"a": None}


def test_restore_keeps_null_where_the_schema_allows_it():
    schema = {"type": "object", "required": [], "properties": {
        "t": {"type": ["string", "null"]}, "c": {"const": None}, "e": {"enum": ["a", None]},
        "n": {"anyOf": [{"type": "integer"}, {"type": "null"}]}, "drop": {"type": "string"}}}
    value = {"t": None, "c": None, "e": None, "n": None, "drop": None}
    assert restore(schema, value) == {"t": None, "c": None, "e": None, "n": None}


def test_restore_keeps_null_for_an_unknown_boolean_subschema():
    schema = {"type": "object", "properties": {"free": True}}
    assert restore(schema, {"free": None}) == {"free": None}


def test_restore_picks_the_union_branch_by_const_discriminator():
    schema = {"anyOf": [
        {"type": "object", "properties": {"kind": {"const": "a"}, "x": {"type": "string"}}, "required": ["kind"]},
        {"type": "object", "properties": {"kind": {"const": "b"}, "y": {"type": "string"}}, "required": ["kind"]}]}
    # branch "b" is chosen by its const, so the optional `y` null is dropped and `x`'s irrelevance does not matter
    assert restore(schema, {"kind": "b", "y": None}) == {"kind": "b"}
    assert restore(schema, {"kind": "a", "x": "v"}) == {"kind": "a", "x": "v"}


def test_restore_picks_the_union_branch_by_enum_discriminator():
    schema = {"oneOf": [
        {"type": "object", "properties": {"kind": {"enum": ["a", "a2"]}, "x": {"type": "string"}}, "required": ["kind"]},
        {"type": "object", "properties": {"kind": {"enum": ["b"]}, "y": {"type": "integer"},
                                          "flag": True}, "required": ["kind"]}]}
    assert restore(schema, {"kind": "b", "y": None, "flag": 1}) == {"kind": "b", "flag": 1}
    assert restore(schema, {"kind": "a2", "x": None}) == {"kind": "a2"}


def test_restore_gives_up_on_runaway_recursion_without_error():
    schema = {"type": "object", "properties": {"next": {"$ref": "#"}, "tag": {"type": "string"}}}
    value: dict = {"tag": None}
    for _ in range(60):
        value = {"next": value, "tag": "t"}
    out = restore(schema, value)
    for _ in range(60):
        out = out["next"]
    # past the depth cap the subtree comes back untouched: the optional null at the bottom is not stripped
    assert out == {"tag": None}


def test_restore_cyclic_ref_chain_terminates():
    schema = {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}, "required": ["a"],
              "$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}}}
    assert restore(schema, {"a": {"k": None}}) == {"a": {"k": None}}


def test_nested_unsupported_keyword_still_refused():
    with pytest.raises(UnsupportedFeature):
        to_strict({"type": "object", "properties": {"a": {"not": {"type": "string"}}}})
