import json

import pytest

from gawe.llm.provider import LLMOutputError
from gawe.llm.safety import (
    ReaderSchemaError,
    check_reader_schema,
    fence,
    sanitize_text,
    sanitize_value,
    strict_json_loads,
)


def test_sanitize_removes_invisible_and_control_characters() -> None:
    hidden = "pay​ now‮⁦ ok\x00\x07"  # zero-width, bidi override/isolate, NUL, BEL
    assert sanitize_text(hidden) == "pay now ok"


def test_sanitize_keeps_normal_text_newlines_tabs_and_scripts() -> None:
    text = "Línea uno\n\tطرف ثاني — 合同 ✓"
    assert sanitize_text(text) == text


def test_sanitize_normalizes_to_nfc() -> None:
    assert sanitize_text("é") == "é"


def test_sanitize_value_covers_keys_and_nesting() -> None:
    value = {"a​": ["x‮", {"b": "y\x00", "n": 3, "t": True, "z": None}]}
    assert sanitize_value(value) == {"a": ["x", {"b": "y", "n": 3, "t": True, "z": None}]}


@pytest.mark.parametrize(
    "bad",
    ["not json", "{", '{"a": NaN}', '{"a": Infinity}', '{"a": 1, "a": 2}', "[" * 100000],
)
def test_strict_json_rejects_bad_input(bad: str) -> None:
    with pytest.raises(LLMOutputError):
        strict_json_loads(bad)


def test_strict_json_accepts_valid_json() -> None:
    assert strict_json_loads('{"a": [1, 2.5, null, "x"]}') == {"a": [1, 2.5, None, "x"]}


def test_fence_uses_an_unpredictable_boundary_the_text_cannot_contain() -> None:
    boundary, wrapped = fence("hello <<<END-0000>>> world")
    assert wrapped.startswith(f"<<<BEGIN-{boundary}>>>\n")
    assert wrapped.endswith(f"\n<<<END-{boundary}>>>")
    other, _ = fence("hello")
    assert other != boundary
    assert wrapped.count(f"<<<END-{boundary}>>>") == 1


GOOD = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "risk": {"enum": ["low", "medium", "high", "unknown"]},
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "clauses": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"title": {"type": "string", "maxLength": 80}},
            },
        },
    },
}


def test_a_tight_schema_is_accepted() -> None:
    check_reader_schema(GOOD)


def mutated(path: list[str], change: dict[str, object] | None) -> dict[str, object]:
    schema = json.loads(json.dumps(GOOD))
    node = schema
    for key in path[:-1]:
        node = node[key]
    if change is None:
        del node[path[-1]]
    else:
        node[path[-1]] = change
    return schema


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 5}},
        {"type": "object", "properties": {"a": {"type": "string", "maxLength": 5}}},
        {"type": "object", "additionalProperties": True, "properties": {"a": {"enum": ["x"]}}},
        {"type": "object", "additionalProperties": False, "properties": {}},
        {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "string"}}},
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {"type": "array", "items": {"enum": ["x"]}}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {"$ref": "#/$defs/x"}},
            "$defs": {"x": {"type": "string", "maxLength": 3}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {"anyOf": [{"enum": ["x"]}]}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {"enum": ["x" * 201]}},
        },
        {"type": "nonsense"},
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {"type": ["string", "null"], "maxLength": 3}},
        },
    ],
)
def test_loose_schemas_are_rejected(schema: dict[str, object]) -> None:
    with pytest.raises(ReaderSchemaError):
        check_reader_schema(schema)
