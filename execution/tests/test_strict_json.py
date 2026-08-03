"""Issue #78 / PR #100: the strict JSON input loader -- duplicate keys
(nested included), UTF-8 / BOM, size / depth / string / array / object limits
and non-finite numbers all fail as ONE stable error type."""

import json

import pytest

from execution import strict_json
from execution.strict_json import (
    MAX_ARRAY_ITEMS,
    MAX_INPUT_FILE_BYTES,
    MAX_JSON_DEPTH,
    MAX_OBJECT_KEYS,
    MAX_STRING_LENGTH,
    StrictJsonError,
    load_strict_json_file,
    parse_strict_json,
)


class MyError(ValueError):
    pass


def load(tmp_path, data: bytes, error_cls=MyError):
    path = tmp_path / "input.json"
    path.write_bytes(data)
    return load_strict_json_file(path, description="test input", error_cls=error_cls)


def test_valid_json_including_japanese_loads(tmp_path):
    payload = {"名前": "シャドウ", "nested": {"ok": True}, "list": [1, 2, 3]}
    assert load(tmp_path, json.dumps(payload, ensure_ascii=False).encode("utf-8")) == payload


def test_root_duplicate_key_is_rejected(tmp_path):
    with pytest.raises(MyError, match="duplicate object key 'a'"):
        load(tmp_path, b'{"a": 1, "a": 2}')


def test_nested_duplicate_key_is_rejected(tmp_path):
    with pytest.raises(MyError, match="duplicate object key 'x'"):
        load(tmp_path, b'{"outer": {"inner": [{"x": 1, "x": 2}]}}')


def test_invalid_utf8_is_a_stable_error(tmp_path):
    with pytest.raises(MyError, match="not valid UTF-8"):
        load(tmp_path, b'{"a": "\xff\xfe"}')


def test_bom_is_rejected_explicitly(tmp_path):
    with pytest.raises(MyError, match="BOM"):
        load(tmp_path, b"\xef\xbb\xbf" + b'{"a": 1}')


def test_oversized_file_is_rejected_before_parsing(tmp_path):
    blob = b'{"a": "' + b"x" * MAX_INPUT_FILE_BYTES + b'"}'
    with pytest.raises(MyError, match="maximum accepted size"):
        load(tmp_path, blob)


def test_nan_and_infinity_are_rejected(tmp_path):
    for literal in (b"NaN", b"Infinity", b"-Infinity"):
        with pytest.raises(MyError, match="non-finite"):
            load(tmp_path, b'{"a": ' + literal + b"}")


def test_deep_nesting_is_rejected(tmp_path):
    deep = b"[" * (MAX_JSON_DEPTH + 1) + b"1" + b"]" * (MAX_JSON_DEPTH + 1)
    with pytest.raises(MyError, match="depth|deep"):
        load(tmp_path, deep)


def test_oversized_string_array_and_object_are_rejected():
    with pytest.raises(StrictJsonError, match="string exceeds"):
        parse_strict_json(json.dumps({"a": "x" * (MAX_STRING_LENGTH + 1)}), description="t")
    with pytest.raises(StrictJsonError, match="array exceeds"):
        parse_strict_json(json.dumps({"a": list(range(MAX_ARRAY_ITEMS + 1))}), description="t")
    with pytest.raises(StrictJsonError, match="object exceeds"):
        parse_strict_json(
            json.dumps({str(index): index for index in range(MAX_OBJECT_KEYS + 1)}),
            description="t",
        )


def test_missing_file_is_the_caller_error_type(tmp_path):
    with pytest.raises(MyError, match="cannot read"):
        load_strict_json_file(tmp_path / "missing.json", description="test input", error_cls=MyError)


def test_limits_are_documented_constants():
    assert strict_json.MAX_INPUT_FILE_BYTES == 262_144
    assert strict_json.MAX_JSON_DEPTH == 32
    assert strict_json.MAX_STRING_LENGTH == 4_096
    assert strict_json.MAX_ARRAY_ITEMS == 1_024
    assert strict_json.MAX_OBJECT_KEYS == 256
