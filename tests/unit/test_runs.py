from datetime import UTC, datetime

import pytest

from gawe.core.runs import InMemoryRunStore, RunNotFoundError, RunRecord, StaleRunError
from gawe.core.statemachine import IllegalTransitionError, RunStatus

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def run(run_id: str = "r1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        tenant_id="t",
        acting_user_id="u",
        roles=("a",),
        objective="o",
        profile="p",
        created_at=NOW,
    )


def test_add_get_and_duplicate() -> None:
    store = InMemoryRunStore()
    store.add(run())
    assert store.get("r1").status is RunStatus.CREATED
    with pytest.raises(ValueError):
        store.add(run())
    with pytest.raises(RunNotFoundError):
        store.get("nope")


def test_transition_is_compare_and_set() -> None:
    store = InMemoryRunStore()
    store.add(run())
    store.transition("r1", expected=RunStatus.CREATED, to=RunStatus.PLANNING)
    with pytest.raises(StaleRunError):
        store.transition("r1", expected=RunStatus.CREATED, to=RunStatus.PLANNING)


def test_illegal_transition_is_refused() -> None:
    store = InMemoryRunStore()
    store.add(run())
    with pytest.raises(IllegalTransitionError):
        store.transition("r1", expected=RunStatus.CREATED, to=RunStatus.COMPLETED)


def test_terminal_runs_cannot_move() -> None:
    store = InMemoryRunStore()
    store.add(run())
    store.transition("r1", expected=RunStatus.CREATED, to=RunStatus.CANCELLED)
    with pytest.raises(IllegalTransitionError):
        store.transition("r1", expected=RunStatus.CANCELLED, to=RunStatus.RUNNING)


def test_update_rejects_unknown_and_protected_fields() -> None:
    store = InMemoryRunStore()
    store.add(run())
    with pytest.raises(ValueError):
        store.update("r1", status=RunStatus.COMPLETED)
    with pytest.raises(ValueError):
        store.update("r1", acting_user_id="mallory")
    assert store.update("r1", error="x").error == "x"


def test_unknown_run_update_raises() -> None:
    with pytest.raises(RunNotFoundError):
        InMemoryRunStore().update("nope", error="x")
