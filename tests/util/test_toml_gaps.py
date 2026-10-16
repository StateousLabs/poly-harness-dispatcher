import tomllib

import pytest

from phd._util import toml as _toml


def test_inline_tables_inside_mixed_arrays_round_trip():
    data = {"items": [1, {"a": "x", "b": [True, 2.5]}]}
    text = _toml.dumps(data)
    assert "{ a = " in text
    assert tomllib.loads(text) == data


def test_unsupported_value_type_is_rejected():
    with pytest.raises(TypeError, match="cannot write NoneType to TOML"):
        _toml.dumps({"key": None})
