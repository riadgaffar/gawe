"""M27 S1: bounded, declared snapshots and honest comparison states, without live adapters."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from gawe.core.canonical import action_hash
from gawe.core.record_changes import (
    CapturedField,
    FieldSpec,
    RecordObservation,
    RecordSnapshot,
    RefusalDiagnostic,
    SnapshotSpec,
    compare_records,
)

NOW = datetime(2026, 11, 25, tzinfo=UTC)
SPEC = SnapshotSpec(resource="clm.contract", fields=(FieldSpec(path="deal_value", kind="number"),))


def snapshot(value: Any = 45000, **updates: Any) -> RecordSnapshot:
    return RecordSnapshot(
        **{
            "resource": "clm.contract",
            "subject": "C-1001",
            "revision": "v1",
            "captured_at": NOW,
            "fields": (CapturedField(path="deal_value", value=value),),
            **updates,
        }
    )


def observation(value: Any = 150000, **updates: Any) -> RecordObservation:
    return RecordObservation(
        **{
            "resource": "clm.contract",
            "subject": "C-1001",
            "revision": "v2",
            "observed_at": NOW,
            "timing": "at_guard",
            "status": "observed",
            "fields": (CapturedField(path="deal_value", value=value),),
            **updates,
        }
    )


@pytest.mark.parametrize(
    "value,state", [(150000, "changed"), (45000, "unchanged"), (45000.0, "unchanged")]
)
def test_comparison_uses_fields_not_revision(value: Any, state: str) -> None:
    result = compare_records(SPEC, snapshot(), observation(value))
    assert result.state == state
    assert result.rows[0].before.value == 45000
    assert result.rows[0].observed.value == value
    assert result.rows[0].changed is (state == "changed")


def test_no_fields_unavailable_and_missing_are_distinct() -> None:
    assert compare_records(SPEC, None, observation()).state == "snapshot_unavailable"
    assert compare_records(SPEC, snapshot(), None).state == "observation_unavailable"
    for status, state in [
        ("missing", "record_missing"),
        ("unavailable", "observation_unavailable"),
    ]:
        seen = observation(
            status=status,
            revision=None,
            fields=(),
            reason="unreadable" if status == "unavailable" else None,
        )
        assert compare_records(SPEC, snapshot(), seen).state == state
    empty = SnapshotSpec(resource="clm.contract")
    assert (
        compare_records(empty, snapshot(fields=()), observation(fields=())).state
        == "no_fields_tracked"
    )


def test_missing_and_null_are_not_equal() -> None:
    spec = SnapshotSpec(
        resource="clm.contract",
        fields=(FieldSpec(path="x", kind="string", required=False, nullable=True),),
    )
    before = snapshot(fields=(CapturedField(path="x", present=False),))
    after = observation(fields=(CapturedField(path="x", value=None),))
    assert compare_records(spec, before, after).state == "changed"


@pytest.mark.parametrize("value", [True, "45000", None])
def test_declared_numeric_fields_reject_boolean_text_and_null(value: Any) -> None:
    with pytest.raises(ValueError):
        SPEC.validate_fields(snapshot(value))


@pytest.mark.parametrize(
    "value", [{"secret": "record"}, ["text"], "x" * 257, float("nan"), float("inf")]
)
def test_no_arbitrary_record_text_or_nonfinite_numbers(value: Any) -> None:
    with pytest.raises(ValidationError):
        CapturedField(path="deal_value", value=value)


@pytest.mark.parametrize(
    "fields",
    [
        (),
        (CapturedField(path="extra", value=1),),
        (CapturedField(path="deal_value", present=False),),
    ],
)
def test_missing_or_undeclared_fields_are_not_an_empty_diff(fields: Any) -> None:
    with pytest.raises(ValueError):
        compare_records(SPEC, snapshot(), observation(fields=fields))


@pytest.mark.parametrize("updates", [{"resource": "iam.user"}, {"subject": "C-1002"}])
def test_other_record_cannot_be_compared(updates: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        compare_records(SPEC, snapshot(), observation(**updates))


def test_schema_rejects_duplicates_unknown_fields_and_ambiguous_status() -> None:
    with pytest.raises(ValidationError):
        snapshot(fields=(CapturedField(path="x"), CapturedField(path="x")))
    with pytest.raises(ValidationError):
        SnapshotSpec(resource="clm.contract", fields=(SPEC.fields[0], SPEC.fields[0]))
    with pytest.raises(ValidationError):
        snapshot(untrusted_note="private")
    with pytest.raises(ValidationError):
        CapturedField(path="x", present=False, value=1)
    for changes in [{"revision": None}, {"status": "missing"}, {"reason": "unreadable"}]:
        with pytest.raises(ValidationError):
            observation(**changes)
    with pytest.raises(ValidationError):
        observation(status="unavailable", fields=(), revision=None)
    with pytest.raises(ValidationError):
        snapshot(captured_at=datetime(2026, 11, 25))


def test_confirmed_original_requires_provenance() -> None:
    with pytest.raises(ValidationError):
        snapshot(basis="confirmed_original")
    with pytest.raises(ValidationError):
        snapshot(source_action_hash="a" * 64)
    saved = snapshot(basis="confirmed_original", source_action_hash="a" * 64)
    assert RecordSnapshot.model_validate_json(saved.model_dump_json()) == saved


def test_snapshot_changes_cannot_keep_the_authorized_action_hash() -> None:
    def digest(record: RecordSnapshot) -> str:
        return action_hash(
            tool="crm.write_assessment",
            version="4.0.0",
            resolved_args={"record": record.model_dump(mode="json")},
            run_id="r",
            step_id="s",
        )

    original = snapshot()
    for changed in [
        snapshot(150000),
        snapshot(revision="v9"),
        snapshot(subject="C-1002"),
        snapshot(fields=()),
    ]:
        assert digest(changed) != digest(original)
    assert digest(RecordSnapshot.model_validate_json(original.model_dump_json())) == digest(
        original
    )


def test_observation_timing_coverage_and_action_binding_survive_round_trip() -> None:
    diagnostic = RefusalDiagnostic(
        action_hash="a" * 64,
        code="record_changed",
        coverage="partial",
        observations=(observation(timing="after_refusal"),),
    )
    reloaded = RefusalDiagnostic.model_validate_json(diagnostic.model_dump_json())
    assert reloaded == diagnostic
    assert reloaded.observations[0].timing == "after_refusal"
    reloaded.validate_for("a" * 64, {("clm.contract", "C-1001"), ("clm.contract", "C-1002")})
    with pytest.raises(ValueError):
        reloaded.validate_for("b" * 64, {("clm.contract", "C-1001")})
    with pytest.raises(ValueError):
        reloaded.validate_for("a" * 64, {("clm.contract", "C-1002")})
    with pytest.raises(ValueError):
        diagnostic.model_copy(update={"coverage": "complete"}).validate_for(
            "a" * 64, {("clm.contract", "C-1001"), ("clm.contract", "C-1002")}
        )
    with pytest.raises(ValidationError):
        RefusalDiagnostic(
            action_hash="a" * 64,
            code="record_changed",
            coverage="complete",
            observations=(observation(), observation()),
        )


def test_diagnostic_covers_a_full_replay_batch_and_rejects_oversized_evidence() -> None:
    from gawe.apps.event_replay.tools import MAX_BATCH

    observations = tuple(observation(subject=f"event-{i}") for i in range(MAX_BATCH))
    diagnostic = RefusalDiagnostic(
        action_hash="a" * 64,
        code="event_changed",
        coverage="complete",
        observations=observations,
    )
    diagnostic.validate_for("a" * 64, {(o.resource, o.subject) for o in observations})
    with pytest.raises(ValidationError):
        RefusalDiagnostic(
            action_hash="a" * 64,
            code="event_changed",
            coverage="complete",
            observations=(*observations, observation(subject="extra")),
        )
