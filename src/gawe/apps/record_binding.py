"""Execution schemas for writes bound to a system-of-record revision."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from gawe.core.models import ToolManifest
from gawe.core.record_changes import CapturedField, FieldSpec, RecordSnapshot, SnapshotSpec


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


# M27 declarations: selected fields, never a whole record or a document's prose.

CONTRACT_FIELDS = SnapshotSpec(
    resource="clm.contract", fields=(FieldSpec(path="deal_value", kind="number"),)
)
ACCOUNT_FIELDS = SnapshotSpec(resource="iam.user")  # Policy reads ticket/config/history instead.
EVENT_FIELDS = SnapshotSpec(
    resource="billing.event",
    fields=(FieldSpec(path="customer", kind="string"), FieldSpec(path="amount", kind="number")),
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def capture(
    spec: SnapshotSpec,
    subject: str,
    record: Mapping[str, Any],
    revision: str,
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    snapshot = RecordSnapshot(
        resource=spec.resource,
        subject=subject,
        revision=revision,
        captured_at=clock(),
        fields=tuple(
            CapturedField(path=f.path, present=f.path in record, value=record.get(f.path))
            for f in spec.fields
        ),
    )
    spec.validate_fields(snapshot)
    return snapshot.model_dump(mode="json")


def with_snapshots(
    manifest: ToolManifest, version: str, *, maximum: int = 1, minimum: int = 1
) -> ToolManifest:
    """New execution schema only. Old schemas and model proposal fields remain unchanged."""
    schema = RecordSnapshot.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(value: Any) -> Any:
        if isinstance(value, list):
            return [inline(v) for v in value]
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(definitions[value["$ref"].split("/")[-1]])
            return {k: inline(v) for k, v in value.items()}
        return value

    inputs = manifest.input_schema
    return manifest.model_copy(
        update={
            "version": version,
            "input_schema": {
                **inputs,
                "properties": {
                    **inputs["properties"],
                    "snapshots": {
                        "type": "array",
                        "minItems": minimum,
                        "maxItems": maximum,
                        "items": inline(schema),
                    },
                },
                "required": [*inputs["required"], "snapshots"],
            },
            "field_classes": {**(manifest.field_classes or {}), "snapshots": "typed"},
        }
    )
