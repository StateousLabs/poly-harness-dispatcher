import pytest

from phd.spec.output_schema import MAX_MESSAGE, answer_instruction, extract_json, schema_error, validate_answer

SCHEMA = {"type": "object", "properties": {"event": {"enum": ["done", "blocked"]}, "n": {"type": "integer"}},
          "required": ["event"], "additionalProperties": False}


def test_schema_error_is_none_for_a_valid_schema():
    assert schema_error(SCHEMA) is None


def test_schema_error_reports_the_problem():
    message = schema_error({"type": "objekt"})
    assert message and "objekt" in message


def test_draft_comes_from_dollar_schema():
    assert schema_error({"$schema": "http://json-schema.org/draft-07/schema#", "type": "string"}) is None


@pytest.mark.parametrize("text,expected", [
    ('{"event": "done"}', {"event": "done"}),
    ("  [1, 2]\n", [1, 2]),
    ('Here you go:\n```json\n{"event": "done"}\n```\n', {"event": "done"}),
    ('```\n"x"\n```', "x"),
])
def test_extract_json(text, expected):
    assert extract_json(text) == expected


@pytest.mark.parametrize("text", ["not json", "```json\n{}\n```\nand\n```json\n{}\n```", "```json\nnope\n```"])
def test_extract_json_rejects(text):
    with pytest.raises(ValueError, match="final answer is not JSON"):
        extract_json(text)


def test_validate_answer_accepts_a_match():
    assert validate_answer(SCHEMA, {"event": "done", "n": 1}) is None


def test_validate_answer_reports_the_path():
    message = validate_answer(SCHEMA, {"event": "nope"})
    assert message.startswith("$.event: ") and "nope" in message


def test_validate_answer_is_capped():
    message = validate_answer({"type": "string", "maxLength": 1}, "x" * 5000)
    assert len(message) <= MAX_MESSAGE


def test_answer_instruction_carries_the_schema():
    text = answer_instruction(SCHEMA)
    assert "must be only a JSON value that matches this JSON Schema" in text and '"blocked"' in text


def test_validate_answer_scrubs_before_capping():
    secret = "SECRETVALUE"
    value = {"event": "a" * 480 + secret}
    message = validate_answer(SCHEMA, value, scrub=lambda text: text.replace(secret, "[X]"))
    assert "SECRET" not in message and len(message) <= MAX_MESSAGE
    assert "SECRET" in validate_answer(SCHEMA, value)  # without scrub the cap would split it, not hide it


@pytest.mark.parametrize("schema", [
    {"$ref": "#/$defs/missing"},
    {"properties": {"a": {"$ref": "#/nope"}}},
    {"$ref": "https://example.com/s.json"},
])
def test_schema_error_reports_an_unresolvable_ref(schema):
    message = schema_error(schema)
    assert message is not None and message.startswith("unresolvable $ref: ") and len(message) <= MAX_MESSAGE


def test_schema_error_accepts_a_resolvable_ref():
    assert schema_error({"$defs": {"e": {"enum": ["done"]}}, "properties": {"event": {"$ref": "#/$defs/e"}}}) is None


def test_validate_answer_does_not_raise_for_an_unresolvable_ref():
    message = validate_answer({"$ref": "#/$defs/SECRETVALUE"}, {"event": "done"},
                              scrub=lambda text: text.replace("SECRETVALUE", "[X]"))
    assert message.startswith("unresolvable $ref: ") and "SECRETVALUE" not in message
    assert len(message) <= MAX_MESSAGE


@pytest.mark.parametrize("tag", ["JSON", "Json"])
def test_extract_json_fence_tag_is_case_insensitive(tag):
    assert extract_json(f'Here:\n```{tag}\n{{"event": "done"}}\n```') == {"event": "done"}
