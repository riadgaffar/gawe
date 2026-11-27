"""M26: the same insert-once, compare-and-set, tenant-scoped undo store on both backends."""

from typing import Any

import pytest

from gawe.core.undo import InMemoryUndoStore, UndoConflictError, UndoRecord, UndoStore, settled
from tests.contract.conftest import TENANT, Backend
from tests.flow import T0


@pytest.fixture
def undo(backend: Backend) -> UndoStore:
    if backend.name == "memory":
        return InMemoryUndoStore()
    from gawe.db.undo_store import PostgresUndoStore

    return PostgresUndoStore(backend.steps._bind)  # type: ignore[attr-defined]


def make(backend: Backend, **updates: Any) -> UndoRecord:
    return UndoRecord(
        **{
            "undo_id": "a" * 64,
            "tenant_id": TENANT,
            "run_id": backend.run_id,
            "step_id": "write",
            "tool": "crm.write_assessment",
            "tool_version": "3.0.0",
            "action_hash": "b" * 64,
            "operation_key": "c" * 64,
            "subject": {"contract_id": "C-1001"},
            "prior": {"risk_level": "high", "summary": "earlier"},
            "bound_version": "v1",
            "created_at": T0,
            **updates,
        }
    )


def test_a_record_round_trips_and_is_added_once(backend: Backend, undo: UndoStore) -> None:
    record = make(backend)
    assert undo.add(record) is True
    assert undo.add(record) is False
    assert undo.get(TENANT, record.undo_id) == record
    assert undo.for_run(TENANT, backend.run_id) == [record]
    with pytest.raises(UndoConflictError):
        undo.add(make(backend, prior=None))


def test_arming_is_compare_and_set(backend: Backend, undo: UndoStore) -> None:
    record = make(backend)
    undo.add(record)
    armed = settled(record, "agreement", {"result_version": "v2"})
    assert armed is not None
    assert undo.replace(armed, expected_revision=0) == armed
    assert undo.get(TENANT, record.undo_id) == armed
    with pytest.raises(UndoConflictError):
        undo.replace(armed, expected_revision=0)
    with pytest.raises(UndoConflictError):  # identity never changes
        undo.replace(
            armed.model_copy(update={"bound_version": "v9", "revision": 2}), expected_revision=1
        )


def test_unavailable_keeps_its_reason(backend: Backend, undo: UndoStore) -> None:
    record = make(backend, undo_id="d" * 64)
    undo.add(record)
    gone = settled(record, "unknown", {})
    assert gone is not None
    undo.replace(gone, expected_revision=0)
    stored = undo.get(TENANT, record.undo_id)
    assert stored is not None
    assert (stored.state, stored.reason) == ("unavailable", "outcome_unknown")


def test_another_tenant_sees_nothing(backend: Backend, undo: UndoStore) -> None:
    record = make(backend)
    undo.add(record)
    other = "00000000-0000-0000-0000-0000000000b2"
    assert undo.get(other, record.undo_id) is None
    assert undo.for_run(other, backend.run_id) == []


@pytest.mark.parametrize(
    "state,reason", [("requested", None), ("used", None), ("failed", "record_changed")]
)
def test_reversal_states_round_trip(
    backend: Backend, undo: UndoStore, state: str, reason: str | None
) -> None:
    record = make(backend, state="armed", result_version="v2")
    undo.add(record)
    changed = UndoRecord(**{**record.model_dump(), "state": state, "reason": reason, "revision": 1})
    undo.replace(changed, expected_revision=0)
    assert undo.get(TENANT, record.undo_id) == changed


def test_count_snapshot_tracks_states_reasons_and_tenant(backend: Backend, undo: UndoStore) -> None:
    assert undo.counts(TENANT) == {}
    first = make(backend, state="armed", result_version="v2")
    undo.add(first)
    undo.add(
        make(
            backend,
            undo_id="d" * 64,
            step_id="other",
            state="unavailable",
            reason="outcome_unknown",
        )
    )
    undo.add(
        make(
            backend,
            undo_id="e" * 64,
            step_id="failed",
            state="failed",
            result_version="v3",
            reason="record_changed",
        )
    )
    assert undo.counts(TENANT) == {
        ("armed", None): 1,
        ("unavailable", "outcome_unknown"): 1,
        ("failed", "record_changed"): 1,
    }
    undo.replace(first.model_copy(update={"state": "used", "revision": 1}), expected_revision=0)
    assert undo.counts(TENANT) == {
        ("used", None): 1,
        ("unavailable", "outcome_unknown"): 1,
        ("failed", "record_changed"): 1,
    }
    assert undo.counts("00000000-0000-0000-0000-0000000000b2") == {}


def test_original_snapshots_round_trip_and_cannot_change(backend: Backend, undo: UndoStore) -> None:
    from tests.unit.test_record_changes import snapshot

    record = make(backend, tool_version="4.0.0", snapshots=(snapshot(),))
    undo.add(record)
    loaded = undo.get(TENANT, record.undo_id)
    assert loaded == record and loaded.snapshots == record.snapshots
    armed = settled(loaded, "agreement", {"result_version": "v2"})
    assert armed is not None
    undo.replace(armed, expected_revision=0)
    assert undo.get(TENANT, record.undo_id).snapshots == record.snapshots
    with pytest.raises(UndoConflictError):
        undo.replace(armed.model_copy(update={"revision": 2, "snapshots": ()}), expected_revision=1)
    with pytest.raises(UndoConflictError):
        undo.add(make(backend, tool_version="4.0.0", snapshots=(snapshot(999),)))
