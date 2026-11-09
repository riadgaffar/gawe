import json

from gawe.apps.contract_review.reader_schema import SCHEMA as CONTRACT_SCHEMA
from gawe.apps.it_access.reader_schema import SCHEMA as IT_SCHEMA
from gawe.core.models import Plan
from gawe.llm.schema_adapt import adapt_schema

UNSUPPORTED = ("maxLength", "minLength", "maxItems", "minimum", "maximum", "pattern", "format")


def test_bounds_move_into_descriptions() -> None:
    out = adapt_schema(CONTRACT_SCHEMA)
    assert out is not None
    text = json.dumps(out)
    assert not any(f'"{word}"' in text for word in UNSUPPORTED)
    summary = out["properties"]["summary"]
    assert summary["type"] == "string" and "maxLength=300" in summary["description"]
    assert "maxItems=6" in out["properties"]["parties"]["description"]


def test_structure_is_preserved() -> None:
    out = adapt_schema(CONTRACT_SCHEMA)
    assert out is not None
    assert out["required"] == CONTRACT_SCHEMA["required"]
    assert out["properties"]["risk_level"] == CONTRACT_SCHEMA["properties"]["risk_level"]
    assert out["additionalProperties"] is False
    assert adapt_schema(IT_SCHEMA) is not None


def test_property_names_that_look_like_keywords_survive() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"format": {"type": "string", "maxLength": 3}, "pattern": {"type": "string"}},
    }
    out = adapt_schema(schema)
    assert out is not None and set(out["properties"]) == {"format", "pattern"}
    assert '"maxLength"' not in json.dumps(out)


def test_open_objects_cannot_be_adapted() -> None:
    assert adapt_schema({"type": "object", "properties": {}}) is None
    nested = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"x": {"type": "object", "additionalProperties": True}},
    }
    assert adapt_schema(nested) is None


def test_the_planner_schema_falls_back_to_plain_json() -> None:
    assert adapt_schema(Plan.model_json_schema()) is None


def test_input_is_not_mutated() -> None:
    before = json.dumps(CONTRACT_SCHEMA, sort_keys=True)
    adapt_schema(CONTRACT_SCHEMA)
    assert json.dumps(CONTRACT_SCHEMA, sort_keys=True) == before
