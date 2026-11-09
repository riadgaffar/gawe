"""Adapting a JSON Schema for the Anthropic structured-output feature.

That feature only accepts a subset of JSON Schema: no length, size or numeric bounds, and every
object must be closed (`additionalProperties: false`). Our schemas carry those bounds on purpose
(the Reader's schema is rejected without them), and they are still enforced by us after the model
answers. So for the API call each bound is removed from the schema and written into the field's
`description` instead, which keeps the model informed without the API refusing the schema.

A schema with an open object (for example a plan step's free-form `args`) cannot be sent this way.
`adapt_schema` returns None for those and the caller asks for plain JSON instead.
"""

from typing import Any

_DROPPED = frozenset(
    {
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)
_SILENT = frozenset({"title", "examples"})  # removed without a hint; they carry no constraint
_NAME_MAPS = frozenset({"properties", "$defs", "definitions"})


class _OpenObject(Exception):
    pass


def adapt_schema(schema: Any) -> dict[str, Any] | None:
    """The API-safe version of `schema`, or None if it contains an object that is not closed."""
    try:
        adapted = _walk(schema)
    except _OpenObject:
        return None
    return adapted if isinstance(adapted, dict) else None


def _walk(node: Any) -> Any:
    if isinstance(node, list):
        return [_walk(item) for item in node]
    if not isinstance(node, dict):
        return node
    if node.get("type") == "object" and node.get("additionalProperties") is not False:
        raise _OpenObject
    out: dict[str, Any] = {}
    hints: list[str] = []
    for key, value in node.items():
        if key in _NAME_MAPS and isinstance(value, dict):
            out[key] = {
                name: _walk(child) for name, child in value.items()
            }  # names are not keywords
        elif key in _SILENT:
            continue
        elif key in _DROPPED or (key == "minItems" and isinstance(value, int) and value > 1):
            hints.append(f"{key}={value}")
        else:
            out[key] = _walk(value)
    if hints:
        text = f"Constraints: {', '.join(hints)}."
        out["description"] = f"{out['description']} {text}" if out.get("description") else text
    return out
