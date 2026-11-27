"""M27 contracts for captured policy fields and evidence explaining a refused revision.

Snapshots belong inside prepared, hashed arguments. Diagnostics are observations, never authority
for a retry or an outcome. App adapters declare which fields they capture and observe.
Only bounded scalar values are accepted, never whole records or prose.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictBool, StrictInt, model_validator

from gawe.core.models import FrozenModel

Path = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_.]{0,127}$")]
Text = Annotated[str, Field(strict=True, max_length=256)]
Revision = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
Subject = Annotated[str, Field(strict=True, min_length=1, max_length=256)]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Scalar = (
    Text | StrictBool | StrictInt | Annotated[float, Field(strict=True, allow_inf_nan=False)] | None
)


class CapturedField(FrozenModel):
    path: Path
    present: StrictBool = True
    value: Scalar = None

    @model_validator(mode="after")
    def _absent_has_no_value(self) -> "CapturedField":
        if not self.present and self.value is not None:
            raise ValueError("an absent field has no value")
        return self


class FieldSpec(FrozenModel):
    path: Path
    kind: Literal["string", "number", "boolean"]
    nullable: StrictBool = False
    required: StrictBool = True


class RecordFields(FrozenModel):
    fields: tuple[CapturedField, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def _unique_paths(self) -> "RecordFields":
        if len({f.path for f in self.fields}) != len(self.fields):
            raise ValueError("duplicate captured path")
        return self


class RecordSnapshot(RecordFields):
    resource: Path
    subject: Subject
    revision: Revision
    captured_at: AwareDatetime
    basis: Literal["preparation", "confirmed_original"] = "preparation"
    source_action_hash: Digest | None = None

    @model_validator(mode="after")
    def _source(self) -> "RecordSnapshot":
        if (self.basis == "confirmed_original") != (self.source_action_hash is not None):
            raise ValueError("a confirmed-original snapshot needs its source action hash")
        return self


class SnapshotSpec(FrozenModel):
    resource: Path
    fields: tuple[FieldSpec, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def _unique_paths(self) -> "SnapshotSpec":
        if len({f.path for f in self.fields}) != len(self.fields):
            raise ValueError("duplicate declared path")
        return self

    def validate_fields(self, record: RecordFields) -> None:
        """Require exactly the declared paths; an absent value is explicit, never omitted."""
        actual = {f.path: f for f in record.fields}
        if actual.keys() != {f.path for f in self.fields}:
            raise ValueError("captured paths do not match the declaration")
        for spec in self.fields:
            field = actual[spec.path]
            if not field.present:
                if spec.required:
                    raise ValueError("a required captured field is absent")
                continue
            value = field.value
            if value is None:
                if not spec.nullable:
                    raise ValueError("a non-nullable captured field is null")
                continue
            valid = {
                "string": isinstance(value, str),
                "boolean": isinstance(value, bool),
                "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            }
            if not valid[spec.kind]:
                raise ValueError("captured field type does not match the declaration")


class RecordObservation(RecordFields):
    resource: Path
    subject: Subject
    observed_at: AwareDatetime
    timing: Literal["at_guard", "after_refusal"]
    status: Literal["observed", "missing", "unavailable"]
    revision: Revision | None = None
    reason: Literal["unreadable", "not_recorded", "invalid"] | None = None

    @model_validator(mode="after")
    def _shape(self) -> "RecordObservation":
        if self.status == "observed":
            if self.revision is None or self.reason is not None:
                raise ValueError("observed fields need a revision and no error")
        elif self.revision is not None or self.fields:
            raise ValueError("an unreadable or missing record has no observed fields or revision")
        if (self.status == "unavailable") != (self.reason is not None):
            raise ValueError("only unavailable observations need a reason")
        return self


class RefusalDiagnostic(FrozenModel):
    format: Literal[1] = 1
    action_hash: Digest
    code: Literal["record_changed", "event_changed"]
    coverage: Literal["complete", "partial"]
    # Covers the shipped replay batch bound without accepting unbounded evidence.
    observations: tuple[RecordObservation, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _unique_subjects(self) -> "RefusalDiagnostic":
        ids = {(o.resource, o.subject) for o in self.observations}
        if len(ids) != len(self.observations):
            raise ValueError("duplicate observed subject")
        return self

    def validate_for(self, action_hash: str, targets: set[tuple[str, str]]) -> None:
        """Consumers must bind evidence to the authorized action and its tenant-scoped targets."""
        ids = {(o.resource, o.subject) for o in self.observations}
        if self.action_hash != action_hash or not ids <= targets:
            raise ValueError("diagnostic belongs to another action or target")
        if self.coverage == "complete" and ids != targets:
            raise ValueError("complete diagnostic omits a target")


class FieldChange(FrozenModel):
    before: CapturedField
    observed: CapturedField
    changed: bool


class RecordComparison(FrozenModel):
    state: Literal[
        "changed",
        "unchanged",
        "no_fields_tracked",
        "snapshot_unavailable",
        "observation_unavailable",
        "record_missing",
    ]
    rows: tuple[FieldChange, ...] = ()


def compare_records(
    spec: SnapshotSpec,
    before: RecordSnapshot | None,
    observed: RecordObservation | None,
) -> RecordComparison:
    """Compare selected fields, not revisions. Unchanged fields never permit a refused write.

    Malformed/misbound data raises; an adapter must render it unavailable, not unchanged. Timing
    and coverage stay on the diagnostic and must accompany the presentation of these rows.
    """
    if before is None:
        return RecordComparison(state="snapshot_unavailable")
    if before.resource != spec.resource:
        raise ValueError("snapshot resource does not match its declaration")
    spec.validate_fields(before)
    if observed is None:
        return RecordComparison(state="observation_unavailable")
    if (observed.resource, observed.subject) != (before.resource, before.subject):
        raise ValueError("observation belongs to another resource or subject")
    if observed.status == "unavailable":
        return RecordComparison(state="observation_unavailable")
    if observed.status == "missing":
        return RecordComparison(state="record_missing")
    spec.validate_fields(observed)
    if not spec.fields:
        return RecordComparison(state="no_fields_tracked")
    old, new = {f.path: f for f in before.fields}, {f.path: f for f in observed.fields}
    rows = tuple(
        FieldChange(before=old[f.path], observed=new[f.path], changed=old[f.path] != new[f.path])
        for f in spec.fields
    )
    return RecordComparison(
        state="changed" if any(r.changed for r in rows) else "unchanged", rows=rows
    )
