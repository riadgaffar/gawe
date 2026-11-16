"""Execution schemas for writes bound to a system-of-record revision."""

from typing import Any


def record_schema(field: str) -> dict[str, Any]:
    """The trusted record binding preparation adds; a proposal can never supply it."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {field: {"type": "string", "minLength": 1, "maxLength": 128}},
        "required": [field],
    }


def bound(proposal: dict[str, Any], field: str) -> dict[str, Any]:
    """An execution schema: the proposal plus the required trusted `record`."""
    return {
        **proposal,
        "properties": {**proposal["properties"], "record": record_schema(field)},
        "required": [*proposal["required"], "record"],
    }
