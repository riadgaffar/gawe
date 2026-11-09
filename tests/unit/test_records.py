import threading
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from gawe.core.records import (
    AttemptConflictError,
    ExecutionRecord,
    InMemoryExecutionStore,
    InMemoryStepStore,
    StaleStateError,
    StepNotFoundError,
    StepRecord,
)
from gawe.core.statemachine import IllegalTransitionError, StepStatus

H = "a" * 64
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def step(**kw: object) -> StepRecord:
    base: dict[str, object] = {
        "run_id": "r1",
        "step_id": "s1",
        "tool": "t",
        "tool_version": "1.0.0",
    }
    return StepRecord(**{**base, **kw})  # type: ignore[arg-type]


def test_step_past_policy_requires_hash() -> None:
    with pytest.raises(ValidationError):
        step(status=StepStatus.APPROVED)
    assert step(status=StepStatus.FAILED).action_hash is None
    assert step(status=StepStatus.APPROVED, action_hash=H).status is StepStatus.APPROVED


def test_hash_must_be_sha256_hex() -> None:
    with pytest.raises(ValidationError):
        step(action_hash="xyz")


def test_transition_is_compare_and_set() -> None:
    store = InMemoryStepStore()
    store.add(step())
    store.transition("r1", "s1", expected=StepStatus.PENDING, to=StepStatus.READY)
    with pytest.raises(StaleStateError):
        store.transition("r1", "s1", expected=StepStatus.PENDING, to=StepStatus.READY)


def test_store_refuses_illegal_and_unknown_updates() -> None:
    store = InMemoryStepStore()
    store.add(step())
    with pytest.raises(IllegalTransitionError):
        store.transition("r1", "s1", expected=StepStatus.PENDING, to=StepStatus.EXECUTING)
    with pytest.raises(ValueError, match="cannot update"):
        store.transition("r1", "s1", expected=StepStatus.PENDING, to=StepStatus.READY, tool="other")
    with pytest.raises(StepNotFoundError):
        store.get("r1", "nope")
    with pytest.raises(ValueError, match="already exists"):
        store.add(step())


def test_transition_into_hash_required_state_without_hash_fails() -> None:
    store = InMemoryStepStore()
    store.add(step(status=StepStatus.READY))
    with pytest.raises(ValidationError):
        store.transition("r1", "s1", expected=StepStatus.READY, to=StepStatus.POLICY_CHECKED)
    store.transition(
        "r1", "s1", expected=StepStatus.READY, to=StepStatus.POLICY_CHECKED, action_hash=H
    )


def test_exactly_one_thread_wins_a_transition() -> None:
    store = InMemoryStepStore()
    store.add(step(status=StepStatus.APPROVED, action_hash=H))
    wins: list[int] = []

    def go(n: int) -> None:
        try:
            store.transition("r1", "s1", expected=StepStatus.APPROVED, to=StepStatus.EXECUTING)
            wins.append(n)
        except StaleStateError:
            pass

    threads = [threading.Thread(target=go, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1


def test_execution_attempts_are_sequential() -> None:
    ex = InMemoryExecutionStore()
    ex.start("r1", "s1", attempt=1, idempotency_key="k", now=NOW)
    with pytest.raises(AttemptConflictError):
        ex.start("r1", "s1", attempt=1, idempotency_key="k", now=NOW)
    with pytest.raises(AttemptConflictError):
        ex.start("r1", "s1", attempt=3, idempotency_key="k", now=NOW)
    ex.finish("r1", "s1", attempt=1, status="failed", now=NOW, error="boom")
    with pytest.raises(AttemptConflictError):
        ex.finish("r1", "s1", attempt=1, status="succeeded", now=NOW)
    ex.start("r1", "s1", attempt=2, idempotency_key="k", now=NOW)
    assert [a.status for a in ex.attempts("r1", "s1")] == ["failed", "started"]


def test_execution_record_finished_iff_not_started() -> None:
    with pytest.raises(ValidationError):
        ExecutionRecord(
            run_id="r", step_id="s", attempt=1, idempotency_key="k", status="started",
            started_at=NOW, finished_at=NOW,
        )  # fmt: skip
    with pytest.raises(ValidationError):
        ExecutionRecord(
            run_id="r", step_id="s", attempt=1, idempotency_key="k", status="succeeded",
            started_at=NOW,
        )  # fmt: skip


def test_outputs_round_trip_as_copies() -> None:
    ex = InMemoryExecutionStore()
    assert ex.load_output("r1", "s1") is None
    ex.save_output("r1", "s1", {"a": 1})
    loaded = ex.load_output("r1", "s1")
    assert loaded == {"a": 1}
    assert loaded is not None
    loaded["a"] = 2
    assert ex.load_output("r1", "s1") == {"a": 1}
