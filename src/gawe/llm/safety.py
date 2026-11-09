"""Handling of untrusted text and untrusted model output.

* Text going *into* a model is stripped of characters that are invisible to a human reviewer but
  read by a model (zero-width and bidirectional-control characters, other control characters).
* Documents are wrapped in a boundary that contains a fresh random token, so a document cannot
  fake its own closing marker.
* Model output is parsed strictly (no NaN/Infinity, no duplicate keys) and sanitized the same way.
* A Reader schema must bound everything the model can say: closed objects, length-limited strings,
  size-limited arrays. That keeps the Reader's channel to the Planner narrow.
"""

import json
import secrets
import unicodedata
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from gawe.llm.provider import LLMOutputError

_MAX_CHOICE_LENGTH = 200
_KEEP_CONTROLS = frozenset({"\n", "\t"})
_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_ALLOWED_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_FORBIDDEN_KEYWORDS = frozenset(
    {"$ref", "$dynamicRef", "patternProperties", "if", "then", "else", "oneOf", "anyOf", "allOf"}
)


class ReaderSchemaError(ValueError):
    """A Reader output schema is too loose to be a safe channel."""


def sanitize_text(text: str) -> str:
    """NFC-normalize and drop invisible or control characters (newline and tab are kept)."""
    normalized = unicodedata.normalize("NFC", text)
    return "".join(
        ch
        for ch in normalized
        if ch in _KEEP_CONTROLS or unicodedata.category(ch) not in _STRIP_CATEGORIES
    )


def sanitize_value(value: Any) -> Any:
    """Apply `sanitize_text` to every string (keys included) in a decoded JSON value."""
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {sanitize_text(key): sanitize_value(item) for key, item in value.items()}
    return value


def _reject_constant(name: str) -> Any:
    raise LLMOutputError(f"JSON constant {name} is not allowed")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LLMOutputError("duplicate key in JSON object")
        result[key] = value
    return result


def strict_json_loads(text: str) -> Any:
    try:
        return json.loads(
            text, parse_constant=_reject_constant, object_pairs_hook=_reject_duplicates
        )
    except json.JSONDecodeError as exc:
        raise LLMOutputError("model output is not valid JSON") from exc
    except RecursionError as exc:
        raise LLMOutputError("model output is nested too deeply") from exc


def fence(text: str) -> tuple[str, str]:
    """Wrap untrusted text in a boundary it cannot predict. Returns (boundary, wrapped_text)."""
    while True:
        boundary = secrets.token_hex(12)
        if boundary not in text:
            break
    return boundary, f"<<<BEGIN-{boundary}>>>\n{text}\n<<<END-{boundary}>>>"


def check_reader_schema(schema: Mapping[str, Any]) -> None:
    """Raise ReaderSchemaError unless the schema tightly bounds everything the Reader may emit."""
    try:
        Draft202012Validator.check_schema(dict(schema))
    except SchemaError as exc:
        raise ReaderSchemaError(f"not a valid JSON Schema: {exc.message}") from exc
    if schema.get("type") != "object":
        raise ReaderSchemaError("the top level must be an object")
    _check_node(schema, "$")


def _check_node(node: Any, path: str) -> None:
    if not isinstance(node, Mapping):
        raise ReaderSchemaError(f"{path}: schema must be an object")
    for keyword in _FORBIDDEN_KEYWORDS & set(node):
        raise ReaderSchemaError(f"{path}: '{keyword}' is not allowed in a Reader schema")
    if "enum" in node or "const" in node:
        choices = node.get("enum", [node.get("const")])
        if any(isinstance(c, str) and len(c) > _MAX_CHOICE_LENGTH for c in choices):
            raise ReaderSchemaError(f"{path}: enum/const strings are limited to 200 characters")
        return
    kind = node.get("type")
    if not isinstance(kind, str) or kind not in _ALLOWED_TYPES:
        raise ReaderSchemaError(f"{path}: a single supported 'type' is required")
    if kind == "string" and "maxLength" not in node:
        raise ReaderSchemaError(f"{path}: strings need maxLength")
    if kind == "array":
        if "maxItems" not in node:
            raise ReaderSchemaError(f"{path}: arrays need maxItems")
        _check_node(node.get("items"), f"{path}[]")
    if kind == "object":
        if node.get("additionalProperties") is not False:
            raise ReaderSchemaError(f"{path}: objects need additionalProperties: false")
        properties = node.get("properties")
        if not isinstance(properties, Mapping) or not properties:
            raise ReaderSchemaError(f"{path}: objects need properties")
        for name, child in properties.items():
            _check_node(child, f"{path}.{name}")
